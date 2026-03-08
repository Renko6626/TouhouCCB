# app/api/v1/user.py

from __future__ import annotations

from typing import List, Dict, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_async_session
from app.core.users import current_active_user
from app.models.base import User, Position, Transaction, Outcome, Market
from app.schemas.user import HoldingRead, UserSummary, TransactionRead
from app.services.lmsr import get_current_price

router = APIRouter()


def _rank_title(net_worth: float) -> str:
    """幻想乡称号（可按规则书再调整）"""
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
    """
    计算交易员的实时身价：
    - 净值 (Net Worth) = 现金 - 负债 + 持仓市值
    """
    # 1) 取用户非零持仓，并预加载 outcome->market（避免异步懒加载）
    pos_stmt = (
        select(Position)
        .where(Position.user_id == user.id, Position.amount > 0)
        .options(selectinload(Position.outcome).selectinload(Outcome.market))
    )
    pos_res = await db.execute(pos_stmt)
    positions: List[Position] = pos_res.scalars().all()

    holdings_value = 0.0

    # 2) 逐市场计算 LMSR 价格（先做正确版本，后续可按 market_id 批量优化）
    for pos in positions:
        outcome: Outcome = pos.outcome
        market: Market = outcome.market

        # 取该市场全部 outcomes（为 LMSR shares_list）
        all_outcomes_stmt = (
            select(Outcome)
            .where(Outcome.market_id == market.id)
            .order_by(Outcome.id)
        )
        all_outcomes_res = await db.execute(all_outcomes_stmt)
        all_outcomes: List[Outcome] = all_outcomes_res.scalars().all()

        shares_list = [float(o.total_shares) for o in all_outcomes]
        idx = next((i for i, o in enumerate(all_outcomes) if o.id == outcome.id), None)
        if idx is None:
            # 数据异常：仓位指向的 outcome 不在 market outcomes 内
            continue

        # ✅ 与 market.py 一致：取单个 outcome 的当前价格
        current_price = float(get_current_price(shares_list, idx, float(market.liquidity_b)))
        holdings_value += float(pos.amount) * current_price

    net_worth = float(user.cash) - float(user.debt) + float(holdings_value)
    rank = _rank_title(net_worth)

    return {
        "cash": round(float(user.cash), 2),
        "debt": round(float(user.debt), 2),
        "holdings_value": round(float(holdings_value), 2),
        "net_worth": round(float(net_worth), 2),
        "rank": rank,
    }


@router.get("/holdings", response_model=List[HoldingRead], summary="获取持仓明细")
async def get_my_holdings(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    """
    获取详细持仓（含 market/outcome 信息）
    - 不依赖 user.positions（避免异步懒加载）
    """
    stmt = (
        select(Position)
        .where(Position.user_id == user.id, Position.amount > 0)
        .options(selectinload(Position.outcome).selectinload(Outcome.market))
        .order_by(Position.id.desc())
    )
    res = await db.execute(stmt)
    positions: List[Position] = res.scalars().all()

    results: List[HoldingRead] = []
    for pos in positions:
        # 由于 selectinload，这里不会触发额外 DB IO
        results.append(
            HoldingRead(
                outcome_id=pos.outcome_id,
                outcome_label=pos.outcome.label,
                market_id=pos.outcome.market_id,
                market_title=pos.outcome.market.title,
                amount=round(float(pos.amount), 2),
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
