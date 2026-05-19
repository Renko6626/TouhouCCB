"""共享锁辅助函数——给 market.py 和 liquidation_service.py 共用。

锁顺序约定（必须严格遵守，避免 deadlock）：
1. market 行
2. user 行
3. 该 market 的全部 outcome 行（按 id ASC）
4. position 行（如需要）

所有调用方按这个顺序拿锁。
"""
from __future__ import annotations
from typing import List

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Market, Outcome, User


async def lock_market(db: AsyncSession, market_id: int) -> Market:
    """SELECT FOR UPDATE 市场行。404 if not exists."""
    res = await db.execute(
        select(Market).where(Market.id == market_id).with_for_update()
    )
    market = res.scalars().first()
    if not market:
        raise HTTPException(status_code=404, detail="市场不存在")
    return market


async def lock_user(db: AsyncSession, user_id: int) -> User:
    """SELECT FOR UPDATE 用户行。404 if not exists。"""
    res = await db.execute(
        select(User).where(User.id == user_id).with_for_update()
    )
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


async def lock_outcomes_for_market(db: AsyncSession, market_id: int) -> List[Outcome]:
    """SELECT FOR UPDATE 该市场所有 outcome，按 id ASC。"""
    res = await db.execute(
        select(Outcome)
        .where(Outcome.market_id == market_id)
        .order_by(Outcome.id)
        .with_for_update()
    )
    outcomes = res.scalars().all()
    if not outcomes:
        raise HTTPException(status_code=404, detail="市场选项不存在（数据异常）")
    return outcomes


async def lock_outcome(db: AsyncSession, outcome_id: int) -> Outcome:
    """SELECT FOR UPDATE 单 outcome 行。"""
    res = await db.execute(
        select(Outcome).where(Outcome.id == outcome_id).with_for_update()
    )
    outcome = res.scalars().first()
    if not outcome:
        raise HTTPException(status_code=404, detail="选项不存在")
    return outcome
