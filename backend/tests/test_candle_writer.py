"""candle_writer.compute_candle_rows + upsert_candles 单元测试。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List

import pytest
from sqlmodel import select

from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle
from app.services.candle_writer import (
    compute_candle_rows,
    upsert_candles,
    CANDLE_INTERVALS,
)


async def _seed_market(n_outcomes: int = 2) -> tuple[int, List[int]]:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(title=f"m_{suffix}", status=MarketStatus.TRADING, liquidity_b=10.0)
        s.add(m)
        await s.flush()
        oids = []
        for i in range(n_outcomes):
            o = Outcome(market_id=m.id, label=f"opt_{i}", total_shares=Decimal("0"))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        await s.commit()
        return m.id, oids


def test_compute_candle_rows_two_outcomes():
    """2 outcomes × 4 intervals = 8 行；被交易行 v=shares、n=1；联动行 v=0、n=0。"""
    ts = datetime(2026, 5, 17, 12, 34, 56, tzinfo=timezone.utc)
    outcome_ids = [101, 102]
    new_prices = [0.6, 0.4]
    traded_oid = 102
    shares = Decimal("5")

    rows = compute_candle_rows(
        traded_outcome_id=traded_oid,
        outcome_ids=outcome_ids,
        new_prices=new_prices,
        traded_shares=shares,
        ts=ts,
    )

    assert len(rows) == 8

    traded_rows = [r for r in rows if r["outcome_id"] == 102]
    assert len(traded_rows) == 4
    for r in traded_rows:
        assert r["volume_shares"] == Decimal("5")
        assert r["n_trades"] == 1
        assert r["open_price"] == Decimal("0.4")
        assert r["close_price"] == Decimal("0.4")

    linked_rows = [r for r in rows if r["outcome_id"] == 101]
    assert len(linked_rows) == 4
    for r in linked_rows:
        assert r["volume_shares"] == Decimal("0")
        assert r["n_trades"] == 0
        assert r["open_price"] == Decimal("0.6")


def test_compute_candle_rows_bucket_alignment():
    """bucket_start 应该按 interval 秒数对齐到 floor。"""
    ts = datetime(2026, 5, 17, 12, 34, 56, tzinfo=timezone.utc)
    rows = compute_candle_rows(
        traded_outcome_id=1,
        outcome_ids=[1],
        new_prices=[0.5],
        traded_shares=Decimal("1"),
        ts=ts,
    )
    by_interval = {r["interval"]: r["bucket_start"] for r in rows}
    assert by_interval["10s"] == datetime(2026, 5, 17, 12, 34, 50, tzinfo=timezone.utc)
    assert by_interval["1m"]  == datetime(2026, 5, 17, 12, 34, 0,  tzinfo=timezone.utc)
    assert by_interval["15m"] == datetime(2026, 5, 17, 12, 30, 0,  tzinfo=timezone.utc)
    assert by_interval["1h"]  == datetime(2026, 5, 17, 12, 0,  0,  tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_upsert_single_row_then_read(client):
    _, oids = await _seed_market(n_outcomes=1)
    ts = datetime(2026, 5, 17, 12, 34, 56, tzinfo=timezone.utc)

    async with async_session_maker() as s:
        rows = compute_candle_rows(
            traded_outcome_id=oids[0],
            outcome_ids=oids,
            new_prices=[0.5],
            traded_shares=Decimal("3"),
            ts=ts,
        )
        await upsert_candles(s, rows)
        await s.commit()

        stored = (await s.execute(
            select(OutcomeCandle).where(OutcomeCandle.outcome_id == oids[0])
        )).scalars().all()

    assert len(stored) == 4
    for c in stored:
        assert c.open_price == Decimal("0.50000000")
        assert c.volume_shares == Decimal("3.000000")
        assert c.n_trades == 1


@pytest.mark.asyncio
async def test_upsert_same_bucket_merges_ohlc(client):
    """同 bucket 第二次 UPSERT：O 不变、C 更新、H/L 收紧、V/n 累加。"""
    _, oids = await _seed_market(n_outcomes=1)
    ts = datetime(2026, 5, 17, 12, 34, 0, tzinfo=timezone.utc)

    async with async_session_maker() as s:
        rows1 = compute_candle_rows(oids[0], oids, [0.5], Decimal("2"), ts)
        await upsert_candles(s, rows1)
        rows2 = compute_candle_rows(oids[0], oids, [0.7], Decimal("3"), ts)
        await upsert_candles(s, rows2)
        await s.commit()

        row_1m = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == oids[0],
                OutcomeCandle.interval == "1m",
            )
        )).scalars().first()

    assert row_1m.open_price  == Decimal("0.50000000")
    assert row_1m.close_price == Decimal("0.70000000")
    assert row_1m.high_price  == Decimal("0.70000000")
    assert row_1m.low_price   == Decimal("0.50000000")
    assert row_1m.volume_shares == Decimal("5.000000")
    assert row_1m.n_trades == 2


@pytest.mark.asyncio
async def test_upsert_low_price_collapses_correctly(client):
    """第二次 price 低于第一次 → low_price 收紧到新低。"""
    _, oids = await _seed_market(n_outcomes=1)
    ts = datetime(2026, 5, 17, 12, 34, 0, tzinfo=timezone.utc)

    async with async_session_maker() as s:
        await upsert_candles(s, compute_candle_rows(oids[0], oids, [0.5], Decimal("1"), ts))
        await upsert_candles(s, compute_candle_rows(oids[0], oids, [0.3], Decimal("1"), ts))
        await s.commit()

        row = (await s.execute(
            select(OutcomeCandle).where(
                OutcomeCandle.outcome_id == oids[0],
                OutcomeCandle.interval == "1m",
            )
        )).scalars().first()

    assert row.open_price == Decimal("0.50000000")
    assert row.low_price  == Decimal("0.30000000")
    assert row.high_price == Decimal("0.50000000")
    assert row.close_price == Decimal("0.30000000")


@pytest.mark.asyncio
async def test_upsert_empty_rows_noop(client):
    """空 rows 列表不报错。"""
    async with async_session_maker() as s:
        await upsert_candles(s, [])
        await s.commit()


def test_intervals_constant_matches_spec():
    """CANDLE_INTERVALS 必须是 spec 锁定的 4 档。"""
    assert CANDLE_INTERVALS == [("10s", 10), ("1m", 60), ("15m", 900), ("1h", 3600)]
