"""PostgreSQL validation for the FX candle storage contract.

SQLite covers the aggregation/readiness logic; these exercise the dialect
specifics end to end: ``GREATEST``/``LEAST`` + order-aware ``CASE`` upsert, and
real row-lock serialization of a duplicate/uncertain batch.  Requires
``TEST_PG_DATABASE_URL`` pointing at this task's disposable database.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.fx import FxCandle, FxMarketDataState, FxPair
from app.services.fx.candles import apply_candle_batch, compute_fx_candle_rows

pytestmark = [pytest.mark.pg, pytest.mark.asyncio]

UTC = timezone.utc
BASE = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def _trade(tid, ts, price, *, side="buy", input_amount="2", output_amount="1"):
    return SimpleNamespace(
        id=tid, pair_id=1, created_at=ts, post_price=Decimal(price),
        side=side, input_amount=Decimal(input_amount), output_amount=Decimal(output_amount),
    )


def _at(seconds):
    return BASE + timedelta(seconds=seconds)


async def _seed_pair(factory) -> int:
    async with factory() as db:
        pair = FxPair(currency_code="PGT", currency_name="PG Test")
        db.add(pair)
        await db.flush()
        pair_id = pair.id
        await db.commit()
    return pair_id


async def _read_candle(factory, pair_id, interval, bucket_start):
    async with factory() as db:
        return (await db.execute(select(FxCandle).where(
            FxCandle.pair_id == pair_id,
            FxCandle.interval == interval,
            FxCandle.bucket_start == bucket_start,
        ))).scalars().first()


async def _read_state(factory, pair_id):
    async with factory() as db:
        return (await db.execute(select(FxMarketDataState).where(
            FxMarketDataState.pair_id == pair_id))).scalars().first()


async def test_pg_upsert_merges_out_of_order_batches_once(pg_sessionmaker):
    pair_id = await _seed_pair(pg_sessionmaker)
    later = compute_fx_candle_rows([_trade(2, _at(20), "1.30"), _trade(3, _at(30), "1.40")])
    async with pg_sessionmaker() as db:
        async with db.begin():
            assert await apply_candle_batch(
                db, pair_id=pair_id, expected_trade_id=0, through_trade_id=3,
                rows=later, history_version="gen-1",
            ) is True

    earlier = compute_fx_candle_rows([_trade(1, _at(5), "1.10"), _trade(4, _at(40), "1.50")])
    async with pg_sessionmaker() as db:
        async with db.begin():
            assert await apply_candle_batch(
                db, pair_id=pair_id, expected_trade_id=3, through_trade_id=4,
                rows=earlier, history_version="gen-1",
            ) is True

    candle = await _read_candle(pg_sessionmaker, pair_id, "1m", BASE)
    assert candle.open_price == Decimal("1.10")
    assert candle.close_price == Decimal("1.50")
    assert candle.high_price == Decimal("1.50")
    assert candle.low_price == Decimal("1.10")
    assert candle.gold_volume == Decimal("8")
    assert candle.n_trades == 4
    assert (candle.first_trade_id, candle.last_trade_id) == (1, 4)

    # Duplicate retry after an uncertain commit adds nothing.
    async with pg_sessionmaker() as db:
        async with db.begin():
            assert await apply_candle_batch(
                db, pair_id=pair_id, expected_trade_id=0, through_trade_id=4,
                rows=later + earlier, history_version="gen-1",
            ) is False
    candle = await _read_candle(pg_sessionmaker, pair_id, "1m", BASE)
    assert candle.gold_volume == Decimal("8")
    assert candle.n_trades == 4


async def test_pg_duplicate_batch_serialized_by_row_lock(pg_sessionmaker):
    """Two sessions racing the same batch: the loser sees the committed cursor."""
    pair_id = await _seed_pair(pg_sessionmaker)
    rows = compute_fx_candle_rows([_trade(1, _at(5), "1.10")])
    holding = asyncio.Event()
    release = asyncio.Event()

    async def first():
        async with pg_sessionmaker() as db:
            async with db.begin():
                applied = await apply_candle_batch(
                    db, pair_id=pair_id, expected_trade_id=0, through_trade_id=1,
                    rows=rows, history_version="gen-1",
                )
                holding.set()
                await release.wait()   # keep the state row locked
                return applied

    async def second():
        await holding.wait()
        async with pg_sessionmaker() as db:
            async with db.begin():
                return await apply_candle_batch(
                    db, pair_id=pair_id, expected_trade_id=0, through_trade_id=1,
                    rows=rows, history_version="gen-1",
                )

    async with asyncio.timeout(15):
        first_task = asyncio.create_task(first())
        second_task = asyncio.create_task(second())
        await holding.wait()
        await asyncio.sleep(0.2)       # let the contender reach the lock
        release.set()
        results = await asyncio.gather(first_task, second_task)

    assert sorted(results) == [False, True], results
    assert (await _read_state(pg_sessionmaker, pair_id)).last_trade_id == 1
    candle = await _read_candle(pg_sessionmaker, pair_id, "1m", BASE)
    assert candle.gold_volume == Decimal("2")
    assert candle.n_trades == 1
