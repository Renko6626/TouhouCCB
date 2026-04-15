# app/api/v1/user.py

from __future__ import annotations

from decimal import Decimal
from typing import List, Dict, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_async_session
from app.core.users import current_active_user
from app.models.base import User, Position, Transaction, Outcome, Market
from app.schemas.user import HoldingRead, UserSummary, TransactionRead
from app.services.lmsr import calculate_lmsr_cost, get_current_price, quantize_cost, quantize_price

router = APIRouter()

ZERO = Decimal("0")


def _rank_title(net_worth: Decimal) -> str:
    """幻想乡称号"""
    if net_worth > 50000:
        return "大天狗的座上宾"
    if net_worth > 10000:
        return "守矢神社的VIP"
    if net_worth > 2000:
        return "命莲寺的赞助者"
    if net_worth > 500:
        return "人间之里的小商贩"
    return "初入幻想乡的无名氏"


@router.get("/summary", response_model=UserSummary, summary="获取资产概览")
async def get_user_summary(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    pos_stmt = (
        select(Position)
        .where(Position.user_id == user.id, Position.amount > 0)
        .options(selectinload(Position.outcome).selectinload(Outcome.market))
    )
    pos_res = await db.execute(pos_stmt)
    positions: List[Position] = pos_res.scalars().all()

    holdings_value = ZERO
    total_cost_basis = ZERO

    # 批量查出所有相关市场的 outcomes，避免 N+1
    market_ids = list({pos.outcome.market_id for pos in positions})
    outcomes_by_market: Dict[int, List[Outcome]] = {}
    if market_ids:
        all_outcomes_res = await db.execute(
            select(Outcome)
            .where(Outcome.market_id.in_(market_ids))
            .order_by(Outcome.market_id, Outcome.id)
        )
        for o in all_outcomes_res.scalars().all():
            outcomes_by_market.setdefault(o.market_id, []).append(o)

    for pos in positions:
        outcome: Outcome = pos.outcome
        market: Market = outcome.market
        all_outcomes = outcomes_by_market.get(market.id, [])

        shares_list = [float(o.total_shares) for o in all_outcomes]
        idx = next((i for i, o in enumerate(all_outcomes) if o.id == outcome.id), None)
        if idx is None:
            continue

        # 用 LMSR 成本差计算真实清算价值（考虑滑点），而非 瞬时价格 × 数量
        b = float(market.liquidity_b)
        old_cost = calculate_lmsr_cost(shares_list, b)
        after_sell = list(shares_list)
        after_sell[idx] -= float(pos.amount)
        new_cost = calculate_lmsr_cost(after_sell, b)
        liquidation_value = quantize_cost(old_cost - new_cost)

        holdings_value += liquidation_value
        total_cost_basis += pos.cost_basis

    net_worth = user.cash - user.debt + holdings_value
    unrealized_pnl = holdings_value - total_cost_basis
    rank = _rank_title(net_worth)

    return {
        "cash": user.cash.quantize(Decimal("0.01")),
        "debt": user.debt.quantize(Decimal("0.01")),
        "holdings_value": holdings_value.quantize(Decimal("0.01")),
        "total_cost_basis": total_cost_basis.quantize(Decimal("0.01")),
        "unrealized_pnl": unrealized_pnl.quantize(Decimal("0.01")),
        "net_worth": net_worth.quantize(Decimal("0.01")),
        "rank": rank,
    }


@router.get("/holdings", response_model=List[HoldingRead], summary="获取持仓明细")
async def get_my_holdings(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    stmt = (
        select(Position)
        .where(Position.user_id == user.id, Position.amount > 0)
        .options(selectinload(Position.outcome).selectinload(Outcome.market))
        .order_by(Position.id.desc())
    )
    res = await db.execute(stmt)
    positions: List[Position] = res.scalars().all()

    # 批量查 outcomes 用于 LMSR 定价
    market_ids = list({pos.outcome.market_id for pos in positions})
    outcomes_by_market: Dict[int, List[Outcome]] = {}
    if market_ids:
        all_outcomes_res = await db.execute(
            select(Outcome)
            .where(Outcome.market_id.in_(market_ids))
            .order_by(Outcome.market_id, Outcome.id)
        )
        for o in all_outcomes_res.scalars().all():
            outcomes_by_market.setdefault(o.market_id, []).append(o)

    results: List[HoldingRead] = []
    for pos in positions:
        outcome: Outcome = pos.outcome
        market: Market = outcome.market
        all_outcomes = outcomes_by_market.get(market.id, [])

        shares_list = [float(o.total_shares) for o in all_outcomes]
        idx = next((i for i, o in enumerate(all_outcomes) if o.id == outcome.id), None)

        if idx is not None:
            b = float(market.liquidity_b)
            price_d = quantize_price(get_current_price(shares_list, idx, b))
            # 真实清算价值 = 卖掉全部持仓 LMSR 实际返还金额（含滑点）
            old_cost = calculate_lmsr_cost(shares_list, b)
            after_sell = list(shares_list)
            after_sell[idx] -= float(pos.amount)
            new_cost = calculate_lmsr_cost(after_sell, b)
            market_value = quantize_cost(old_cost - new_cost)
        else:
            price_d = ZERO
            market_value = ZERO

        avg_price = quantize_price(pos.cost_basis / pos.amount) if pos.amount > ZERO else ZERO

        results.append(
            HoldingRead(
                outcome_id=pos.outcome_id,
                outcome_label=pos.outcome.label,
                market_id=pos.outcome.market_id,
                market_title=pos.outcome.market.title,
                amount=pos.amount.quantize(Decimal("0.01")),
                cost_basis=pos.cost_basis.quantize(Decimal("0.01")),
                avg_price=avg_price,
                current_price=price_d,
                market_value=market_value.quantize(Decimal("0.01")),
                unrealized_pnl=(market_value - pos.cost_basis).quantize(Decimal("0.01")),
            )
        )
    return results


@router.get("/transactions", response_model=List[TransactionRead], summary="获取交易历史")
async def get_my_transactions(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    """返回最近 50 条交易记录"""
    stmt = (
        select(Transaction)
        .where(Transaction.user_id == user.id)
        .order_by(Transaction.timestamp.desc())
        .limit(50)
    )
    res = await db.execute(stmt)
    txs: List[Transaction] = res.scalars().all()
    return txs
