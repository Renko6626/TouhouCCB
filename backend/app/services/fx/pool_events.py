"""Append price-source records inside the caller's existing transaction/lock."""
from datetime import datetime
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.fx import FxPoolEvent


def append_pool_event(db: AsyncSession, *, pair_id: int, kind: str,
                      created_at: datetime,
                      pre_reserves: tuple[Decimal, Decimal] | None,
                      post_reserves: tuple[Decimal, Decimal],
                      gold_volume: Decimal, trade_id: int | None = None,
                      audit_id: int | None = None) -> FxPoolEvent:
    """Only add an ORM row; caller owns flush, commit, locks and publication."""
    event = FxPoolEvent(
        pair_id=pair_id, kind=kind, created_at=created_at,
        pre_gold_reserve=None if pre_reserves is None else pre_reserves[0],
        pre_foreign_reserve=None if pre_reserves is None else pre_reserves[1],
        post_gold_reserve=post_reserves[0], post_foreign_reserve=post_reserves[1],
        gold_volume=gold_volume, trade_id=trade_id, audit_id=audit_id,
    )
    db.add(event)
    return event
