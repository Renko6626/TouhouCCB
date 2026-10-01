"""PostgreSQL cross-boundary lock test for the immutable FX history proof.

A producer holds ``FOR UPDATE`` on the pair row while its ``FxTrade`` (born
inside a closed segment) is still uncommitted.  A concurrent cache-miss
``/history`` read must wait on the shared lock, and after the producer commits
it must refuse the segment (503) because the durable cursor is still behind —
never cache an immutable segment that is missing the trade.  A rolled-back
producer must release the lock and let the empty sealed segment be served.

Requires ``TEST_PG_DATABASE_URL`` pointing at this task's disposable database
(``fx_md_api_test``); skipped otherwise.  Never pointed at another worker's DB.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.fx import FxMarketDataState, FxPair, FxTrade
from app.services.fx.market_reads import clear_fx_history_cache, read_fx_history
from app.services.fx.market_state import FX_MARKET_DATA

pytestmark = [pytest.mark.pg, pytest.mark.asyncio]

UTC = timezone.utc


async def _seed_pair(factory) -> int:
    async with factory() as db:
        pair = FxPair(currency_code="PGH", currency_name="PG History")
        db.add(pair)
        await db.flush()
        pid = int(pair.id)
        db.add(FxMarketDataState(pair_id=pid, last_trade_id=0,
                                 history_version="gen-1", history_ready=True))
        await db.commit()
        return pid


def _previous_hour() -> int:
    now = int(datetime.now(UTC).timestamp())
    return now - (now % 3600) - 3600


def _trade(pid: int, trade_id: int, when: datetime) -> FxTrade:
    return FxTrade(
        id=trade_id, pair_id=pid, side="buy",
        input_amount=Decimal("2"), output_amount=Decimal("2"),
        pre_gold_reserve=Decimal("100"), pre_foreign_reserve=Decimal("100"),
        post_gold_reserve=Decimal("100"), post_foreign_reserve=Decimal("100"),
        post_price=Decimal("1.10"), created_at=when,
    )


@pytest.fixture(autouse=True)
def _reset_runtime():
    FX_MARKET_DATA.reset()
    clear_fx_history_cache()
    yield
    FX_MARKET_DATA.reset()
    clear_fx_history_cache()


async def test_history_read_waits_for_inflight_producer_and_refuses_after_commit(pg_sessionmaker):
    pid = await _seed_pair(pg_sessionmaker)
    seg = _previous_hour()
    trade_at = datetime.fromtimestamp(seg + 120, tz=UTC)

    producer = pg_sessionmaker()
    await producer.execute(select(FxPair.id).where(FxPair.id == pid).with_for_update())
    producer.add(_trade(pid, 1, trade_at))
    await producer.flush()  # uncommitted; the pair row lock is held

    reader = pg_sessionmaker()
    task = asyncio.create_task(read_fx_history(reader, pid, "gen-1", "1m", seg))
    try:
        # While the producer is in flight the reader cannot pass the shared
        # lock, so it can never prove-and-cache a segment missing that trade.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)

        await producer.commit()
        with pytest.raises(HTTPException) as exc:
            await asyncio.wait_for(task, timeout=5)
        assert exc.value.status_code == 503
    finally:
        await producer.rollback()
        await producer.close()
        if not task.done():
            task.cancel()
        await reader.close()


async def test_history_read_serves_after_inflight_producer_rolls_back(pg_sessionmaker):
    pid = await _seed_pair(pg_sessionmaker)
    seg = _previous_hour()

    producer = pg_sessionmaker()
    await producer.execute(select(FxPair.id).where(FxPair.id == pid).with_for_update())
    producer.add(_trade(pid, 2, datetime.fromtimestamp(seg + 120, tz=UTC)))
    await producer.flush()

    reader = pg_sessionmaker()
    task = asyncio.create_task(read_fx_history(reader, pid, "gen-1", "1m", seg))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)

        await producer.rollback()
        enc = await asyncio.wait_for(task, timeout=5)
        assert enc["t0"] == seg
        assert enc["v"] == []   # the rolled-back trade never existed
    finally:
        await producer.rollback()
        await producer.close()
        if not task.done():
            task.cancel()
        await reader.close()
