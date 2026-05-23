"""市场 title 门槛检查。

ANY-of 语义：market 若无 required_titles → 通过；否则用户的 user_title 集合
与 required 集合至少有一交集；否则抛 403 MARKET_TITLE_REQUIRED。

只 gate buy；sell/quote/settle 完全不调用本 helper。
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException

from app.models.title import MarketRequiredTitle, UserTitle


async def assert_user_can_trade_market(
    db: AsyncSession, user_id: int, market_id: int,
) -> None:
    required_rows = (await db.execute(
        select(MarketRequiredTitle.title_id).where(
            MarketRequiredTitle.market_id == market_id,
        )
    )).scalars().all()
    required = set(required_rows)
    if not required:
        return  # 无门槛
    owned_rows = (await db.execute(
        select(UserTitle.title_id).where(
            UserTitle.user_id == user_id,
            UserTitle.title_id.in_(required),
        )
    )).scalars().all()
    if owned_rows:
        return  # ANY-of 命中
    raise HTTPException(
        status_code=403,
        detail="MARKET_TITLE_REQUIRED",
    )
