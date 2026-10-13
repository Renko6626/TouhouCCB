"""Prepare pending sourced FX histories after schema upgrade, with writers stopped."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import or_, select
from app.models.audit import AuditEvent
from app.models.fx import FxMarketDataState, FxPair, FxPoolEvent, FxTrade, FxTreasury
from scripts import backfill_fx_candles as backfill


async def select_pending_pairs() -> list[int]:
    trades = select(FxTrade.id).where(FxTrade.pair_id == FxPair.id).exists()
    funds = select(AuditEvent.id).where(
        AuditEvent.ref_table == 'fx_pair', AuditEvent.ref_id == FxPair.id,
        AuditEvent.event_type.in_(['fx_fund', 'fx_withdraw']),
    ).exists()
    events = select(FxPoolEvent.id).where(FxPoolEvent.pair_id == FxPair.id).exists()
    async with backfill.async_session_maker() as db:
        pending = or_(FxMarketDataState.pair_id.is_(None), FxMarketDataState.history_ready.is_(False))
        initialized = select(FxTreasury.id).where(FxTreasury.pair_id == FxPair.id).exists()
        unsupported = select(FxPair.id).outerjoin(
            FxMarketDataState, FxMarketDataState.pair_id == FxPair.id,
        ).where(pending, ~or_(trades, funds, events), or_(FxPair.status != 'draft', initialized))
        missing = list((await db.execute(unsupported)).scalars().all())
        if missing:
            raise ValueError(f'initialized FX pairs lack reconstructible history sources: {missing}')
        statement = select(FxPair.id).outerjoin(
            FxMarketDataState, FxMarketDataState.pair_id == FxPair.id,
        ).where(
            or_(FxMarketDataState.pair_id.is_(None), FxMarketDataState.history_ready.is_(False)),
            or_(trades, funds, events),
        ).order_by(FxPair.id)
        return [int(value) for value in (await db.execute(statement)).scalars().all()]


async def prepare() -> int:
    pair_ids = await select_pending_pairs()
    if not pair_ids:
        print('没有待转换的有来源 FX 历史')
        return 0
    # Validate every selected source chain before the first conversion writes.
    result = await backfill.run(pair_ids, None, dry_run=True)
    if result:
        return result
    return await backfill.run(pair_ids, None, dry_run=False)


async def main() -> int:
    try:
        return await prepare()
    finally:
        await backfill.engine.dispose()


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
