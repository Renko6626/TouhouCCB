"""管理员统计接口：平台资产分布、不平等指标等。"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Dict, List

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.base import Market, Outcome, Position, User
from app.services.lmsr import calculate_lmsr_cost, quantize_cost
from app.services.wealth_stats import compute_wealth_distribution
from app.api.v1.market import SELL_FEE_RATE

logger = logging.getLogger(__name__)
router = APIRouter()

ZERO = Decimal("0")
ONE = Decimal("1")


@router.get("/wealth", summary="平台资产分布统计（仅管理员）")
async def wealth_stats(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    """汇总所有 is_active=True 用户的 net_worth（cash + 持仓清算价 - debt），
    返回均值/中位数/方差/分位数/基尼系数/按称号阈值分桶统计。

    口径与 /user/summary 一致——持仓清算价 = LMSR 全部卖出 gross × (1-fee)。
    单次 query 拉所有 positions + outcomes，Python 内 O(N×P×K) 计算。
    """
    # 1) 拉所有活跃用户
    users = (await db.execute(
        select(User).where(User.is_active == True)  # noqa: E712
    )).scalars().all()
    if not users:
        empty = compute_wealth_distribution(
            [], total_cash=0.0, total_debt=0.0, total_holdings_value=0.0,
        )
        return empty

    # 2) 一次拉所有有效持仓（含 outcome → market eager load）
    positions = (await db.execute(
        select(Position)
        .where(Position.amount > 0)
        .options(selectinload(Position.outcome).selectinload(Outcome.market))
    )).scalars().all()

    # 按 user_id 分组持仓
    positions_by_user: Dict[int, List[Position]] = {}
    for p in positions:
        positions_by_user.setdefault(p.user_id, []).append(p)

    # 3) 拉所有相关 market 的全部 outcomes（LMSR context 用）
    market_ids = {p.outcome.market_id for p in positions}
    outcomes_by_market: Dict[int, List[Outcome]] = {}
    if market_ids:
        all_outcomes = (await db.execute(
            select(Outcome)
            .where(Outcome.market_id.in_(market_ids))
            .order_by(Outcome.market_id, Outcome.id)
        )).scalars().all()
        for o in all_outcomes:
            outcomes_by_market.setdefault(o.market_id, []).append(o)

    # 4) 逐用户算 net_worth
    net_worths: List[float] = []
    total_cash = ZERO
    total_debt = ZERO
    total_holdings = ZERO
    for u in users:
        cash = u.cash
        debt = u.debt
        holdings = ZERO
        for pos in positions_by_user.get(u.id, []):
            market: Market = pos.outcome.market
            ctx_outcomes = outcomes_by_market.get(market.id, [])
            if not ctx_outcomes:
                continue
            shares_list = [float(o.total_shares) for o in ctx_outcomes]
            idx = next(
                (i for i, o in enumerate(ctx_outcomes) if o.id == pos.outcome_id),
                None,
            )
            if idx is None:
                continue
            b = float(market.liquidity_b)
            old_cost = calculate_lmsr_cost(shares_list, b)
            after_sell = list(shares_list)
            after_sell[idx] -= float(pos.amount)
            new_cost = calculate_lmsr_cost(after_sell, b)
            gross = quantize_cost(old_cost - new_cost)
            holdings += quantize_cost(gross * (ONE - SELL_FEE_RATE))

        net_worth = cash + holdings - debt
        net_worths.append(float(net_worth))
        total_cash += cash
        total_debt += debt
        total_holdings += holdings

    logger.info(
        "WEALTH_STATS admin_id=%s n=%s mean=%s",
        admin.id, len(net_worths), sum(net_worths) / len(net_worths) if net_worths else 0,
    )

    return compute_wealth_distribution(
        net_worths,
        total_cash=float(total_cash),
        total_debt=float(total_debt),
        total_holdings_value=float(total_holdings),
    )
