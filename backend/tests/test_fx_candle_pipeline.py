"""FX incremental runtime pipeline tests (real async DB + real service).

Every case targets a concrete fault the runtime must not have: double-counted
volume across a restart or a page boundary, a global instead of per-pair cursor,
a lost post-commit notification, a concurrent same-pair double merge, writes
from a read-only instance, a premature ``history_ready``, or an unsafe
(non-string / lossy) FX history codec.  The storage layer itself is exercised
separately by ``test_fx_candle_storage.py``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.database import async_session_maker
from app.models.fx import FxCandle, FxMarketDataState, FxPair, FxTrade
from app.services.fx.candles import apply_candle_batch, compute_fx_candle_rows
from app.services.fx.market_state import FxMarketDataService

UTC = timezone.utc
NOW = datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)


# ── DB fixtures ───────────────────────────────────────────────────────────
async def _seed_pair(code: str = "TST") -> int:
    async with async_session_maker() as db:
        pair = FxPair(
            currency_code=code,
            currency_name="Test",
            gold_reserve=Decimal("1000"),
            foreign_reserve=Decimal("1000"),
        )
        db.add(pair)
        await db.flush()
        pair_id = int(pair.id)
        await db.commit()
    return pair_id


async def _add_trade(
    pair_id: int,
    *,
    created_at: datetime,
    side: str = "buy",
    price: str = "1.10",
    input_amount: str = "2",
    output_amount: str = "1",
) -> int:
    async with async_session_maker() as db:
        trade = FxTrade(
            pair_id=pair_id,
            side=side,
            input_amount=Decimal(input_amount),
            output_amount=Decimal(output_amount),
            pre_gold_reserve=Decimal("1000"),
            pre_foreign_reserve=Decimal("1000"),
            post_gold_reserve=Decimal("1000"),
            post_foreign_reserve=Decimal("1000"),
            post_price=Decimal(price),
            created_at=created_at,
            source="player",
        )
        db.add(trade)
        await db.flush()
        trade_id = int(trade.id)
        await db.commit()
    return trade_id


async def _read_candle(pair_id: int, interval: str, bucket_start: datetime) -> dict | None:
    async with async_session_maker() as db:
        row = (await db.execute(
            select(FxCandle).where(
                FxCandle.pair_id == pair_id,
                FxCandle.interval == interval,
                FxCandle.bucket_start == bucket_start,
            )
        )).scalars().first()
        if row is None:
            return None
        return {
            "open": row.open_price,
            "high": row.high_price,
            "low": row.low_price,
            "close": row.close_price,
            "volume": row.gold_volume,
            "n": row.n_trades,
            "first_id": row.first_trade_id,
            "last_id": row.last_trade_id,
        }


async def _read_state(pair_id: int) -> dict | None:
    async with async_session_maker() as db:
        row = (await db.execute(
            select(FxMarketDataState).where(FxMarketDataState.pair_id == pair_id)
        )).scalars().first()
        if row is None:
            return None
        return {
            "last_trade_id": int(row.last_trade_id),
            "history_version": str(row.history_version),
            "history_ready": bool(row.history_ready),
        }


async def _count(model) -> int:
    async with async_session_maker() as db:
        return int((await db.execute(select(func.count()).select_from(model))).scalar() or 0)


class _BarrierService(FxMarketDataService):
    """Test seam: park page fetches so two catch-ups are forced to overlap."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fetch_calls = 0
        self.first_fetch = asyncio.Event()
        self.release = asyncio.Event()

    async def _fetch_page(self, pair_id: int, after: int, through: int) -> list:
        rows = await super()._fetch_page(pair_id, after, through)
        self.fetch_calls += 1
        self.first_fetch.set()
        await self.release.wait()
        return rows


# ── per-pair cursor / restart ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_startup_uses_per_pair_cursor_and_restart_does_not_double_count():
    pair_a = await _seed_pair("AAA")
    pair_b = await _seed_pair("BBB")
    a1 = await _add_trade(pair_a, created_at=NOW + timedelta(seconds=5), price="1.10", input_amount="2")
    b1 = await _add_trade(pair_b, created_at=NOW + timedelta(seconds=6), price="2.00", input_amount="5")
    a2 = await _add_trade(pair_a, created_at=NOW + timedelta(seconds=20), price="1.30", input_amount="3")
    assert (a1, b1, a2) == (1, 2, 3)  # interleaved, non-contiguous per pair

    service = FxMarketDataService(async_session_maker, batch_size=2)
    await service.start(write_owner=True)
    try:
        assert service.state(pair_a)["applied_trade_id"] == a2  # pair max, not global max at start
        assert service.state(pair_b)["applied_trade_id"] == b1
        candle = await _read_candle(pair_a, "1m", NOW)
        assert candle["open"] == Decimal("1.10")
        assert candle["close"] == Decimal("1.30")
        assert candle["volume"] == Decimal("5")
        assert candle["n"] == 2
    finally:
        await service.stop()

    restarted = FxMarketDataService(async_session_maker, batch_size=2)
    await restarted.start(write_owner=True)
    try:
        candle = await _read_candle(pair_a, "1m", NOW)
        assert candle["volume"] == Decimal("5")  # replay must not add volume again
        assert candle["n"] == 2
        assert restarted.state(pair_a)["durable_trade_id"] == a2
        rows = restarted.get_candles(pair_a, "1m", NOW, NOW + timedelta(minutes=1))
        assert rows is not None and len(rows) == 1
        assert Decimal(rows[0]["gold_volume"]) == Decimal("5")
    finally:
        await restarted.stop()


# ── page boundary / order ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_multipage_backfill_merges_order_and_volume_once():
    pair = await _seed_pair()
    # Same 1m bucket; created_at order is deliberately not id order.
    await _add_trade(pair, created_at=NOW + timedelta(seconds=30), price="1.30", input_amount="1")
    await _add_trade(pair, created_at=NOW + timedelta(seconds=10), price="1.10", input_amount="2")
    await _add_trade(pair, created_at=NOW + timedelta(seconds=20), price="1.20", input_amount="3")

    service = FxMarketDataService(async_session_maker, batch_size=1)
    await service.start(write_owner=True)
    try:
        candle = await _read_candle(pair, "1m", NOW)
        assert candle["open"] == Decimal("1.10")   # id 2 at :10
        assert candle["close"] == Decimal("1.30")  # id 1 at :30
        assert candle["high"] == Decimal("1.30")
        assert candle["low"] == Decimal("1.10")
        assert candle["volume"] == Decimal("6")
        assert candle["n"] == 3
        assert (candle["first_id"], candle["last_id"]) == (2, 1)

        segment = service.tail(pair, NOW + timedelta(seconds=45))["history_tail"]["1m"]
        assert sum(Decimal(value) for value in segment["v"]) == Decimal("6")
    finally:
        await service.stop()


# ── missed signal / reconciliation ────────────────────────────────────────
@pytest.mark.asyncio
async def test_catch_up_recovers_trades_without_any_notification():
    pair = await _seed_pair()
    service = FxMarketDataService(async_session_maker)
    await service.start(write_owner=True)
    try:
        assert service.state(pair)["history_ready"] is True
        assert service.state(pair)["applied_trade_id"] == 0
        # Committed by another transaction after start and never notified.
        trade_id = await _add_trade(pair, created_at=NOW + timedelta(seconds=5))
        assert await service.catch_up(pair) == trade_id
        await service.flush_once()
        candle = await _read_candle(pair, "1m", NOW)
        assert candle["n"] == 1
        assert candle["volume"] == Decimal("2")
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_background_consumer_catches_up_after_notify():
    pair = await _seed_pair()
    service = FxMarketDataService(async_session_maker)
    service.RECONCILE_INTERVAL = 0.05
    await service.start(write_owner=True)
    try:
        trade_id = await _add_trade(pair, created_at=NOW + timedelta(seconds=5))
        service.notify_committed(pair)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while True:
            state = service.state(pair)
            if state is not None and state["applied_trade_id"] >= trade_id:
                break
            if loop.time() > deadline:
                pytest.fail("background consumer never caught up after notify")
            await asyncio.sleep(0.02)
        assert service.state(pair)["history_ready"] is True
        assert (await _read_state(pair))["last_trade_id"] == trade_id
    finally:
        await service.stop()


# ── per-pair serial consumption ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_concurrent_catch_up_for_same_pair_merges_ring_once():
    pair = await _seed_pair()
    service = _BarrierService(async_session_maker)
    await service.start(write_owner=True)
    try:
        await _add_trade(pair, created_at=NOW + timedelta(seconds=5), input_amount="2")

        # Park the first consumption inside its page fetch so a second call is
        # guaranteed to overlap it; without the per-pair lock both would merge
        # the same rows into the ring and double the volume.
        first = asyncio.create_task(service.catch_up(pair))
        await service.first_fetch.wait()
        second = asyncio.create_task(service.catch_up(pair))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 0.3
        while service.fetch_calls < 2 and loop.time() < deadline:
            await asyncio.sleep(0.005)
        service.release.set()
        await asyncio.gather(first, second)
        await service.flush_once()

        segment = service.tail(pair, NOW + timedelta(seconds=30))["history_tail"]["1m"]
        assert sum(Decimal(value) for value in segment["v"]) == Decimal("2")  # not 4
        candle = await _read_candle(pair, "1m", NOW)
        assert candle["volume"] == Decimal("2")
        assert candle["n"] == 1
    finally:
        await service.stop()


# ── read-only instance ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_read_only_start_and_catch_up_never_write():
    pair = await _seed_pair()
    await _add_trade(pair, created_at=NOW + timedelta(seconds=5))

    service = FxMarketDataService(async_session_maker)
    await service.start(write_owner=False)
    try:
        assert service.state(pair) is None  # no persisted state existed
        assert await _count(FxMarketDataState) == 0
        assert await _count(FxCandle) == 0

        # A reader has no persisted cursor to warm from, so it stays inert and
        # must not create one.
        assert await service.catch_up(pair) == 0
        assert service.state(pair) is None
        assert await _count(FxMarketDataState) == 0
        assert await _count(FxCandle) == 0
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_reader_warms_persisted_ring_without_new_writes():
    pair = await _seed_pair()
    await _add_trade(pair, created_at=NOW + timedelta(seconds=5), input_amount="2")

    writer = FxMarketDataService(async_session_maker)
    await writer.start(write_owner=True)
    await writer.stop()
    state_before = await _count(FxMarketDataState)
    candles_before = await _count(FxCandle)

    reader = FxMarketDataService(async_session_maker)
    await reader.start(write_owner=False)
    try:
        state = reader.state(pair)
        assert state["history_ready"] is True
        assert state["durable_trade_id"] == 1
        rows = reader.get_candles(pair, "1m", NOW, NOW + timedelta(minutes=1))
        assert rows is not None and rows[0]["n_trades"] == 1
        assert await _count(FxMarketDataState) == state_before
        assert await _count(FxCandle) == candles_before
    finally:
        await reader.stop()


# ── readiness transition after an interrupted backfill ────────────────────
@pytest.mark.asyncio
async def test_interrupted_backfill_resumes_and_marks_ready_at_cutoff():
    pair = await _seed_pair()
    t1 = await _add_trade(pair, created_at=NOW + timedelta(seconds=5), input_amount="2")
    t2 = await _add_trade(pair, created_at=NOW + timedelta(seconds=15), input_amount="3")

    # Interrupted initial backfill: trade 1 candles + cursor are durable, but the
    # state was never marked ready and trade 2 was never consumed.
    async with async_session_maker() as db:
        async with db.begin():
            projected = (await db.execute(
                select(
                    FxTrade.id, FxTrade.pair_id, FxTrade.created_at, FxTrade.post_price,
                    FxTrade.side, FxTrade.input_amount, FxTrade.output_amount,
                ).where(FxTrade.id == t1)
            )).all()
            await apply_candle_batch(
                db, pair_id=pair, expected_trade_id=0, through_trade_id=t1,
                rows=compute_fx_candle_rows(projected), history_version="v1",
                history_ready=False,
            )

    service = FxMarketDataService(async_session_maker, batch_size=1)
    await service.start(write_owner=True)
    try:
        state = service.state(pair)
        assert state["history_ready"] is True        # flipped only after trade 2
        assert state["applied_trade_id"] == t2
        candle = await _read_candle(pair, "1m", NOW)
        assert candle["volume"] == Decimal("5")      # trade 1 counted exactly once
        assert candle["n"] == 2
        assert (candle["first_id"], candle["last_id"]) == (t1, t2)
        durable = await _read_state(pair)
        assert durable["history_ready"] is True
        assert durable["last_trade_id"] == t2
        assert durable["history_version"] == "v1"    # generation preserved
    finally:
        await service.stop()


# ── discardable public deltas ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_public_trade_buffer_overflow_invalidates_without_losing_candles():
    pair = await _seed_pair()
    service = FxMarketDataService(async_session_maker)
    service.PUBLIC_TRADE_BUFFER = 2
    await service.start(write_owner=True)
    try:
        await _add_trade(pair, created_at=NOW + timedelta(seconds=1), side="buy",
                         price="1.10", input_amount="2")
        await _add_trade(pair, created_at=NOW + timedelta(seconds=2), side="sell",
                         price="1.20", input_amount="9", output_amount="3.5")
        await service.catch_up(pair)
        deltas, invalidated = service.drain_public_trades(pair)
        assert invalidated is False
        assert [delta["id"] for delta in deltas] == [1, 2]
        assert Decimal(deltas[1]["gold_volume"]) == Decimal("3.5")  # sell uses gold-out
        assert Decimal(deltas[1]["post_price"]) == Decimal("1.20")
        assert deltas[0]["ts"].endswith("+00:00")
        assert isinstance(deltas[0]["post_price"], str)

        await _add_trade(pair, created_at=NOW + timedelta(seconds=3), input_amount="4")
        await _add_trade(pair, created_at=NOW + timedelta(seconds=4), input_amount="5")
        await _add_trade(pair, created_at=NOW + timedelta(seconds=5), input_amount="6")
        await service.catch_up(pair)
        deltas, invalidated = service.drain_public_trades(pair)
        assert invalidated is True                 # partial buffer dropped
        assert deltas == []

        await _add_trade(pair, created_at=NOW + timedelta(seconds=6), input_amount="7")
        await service.catch_up(pair)
        deltas, invalidated = service.drain_public_trades(pair)
        assert invalidated is False
        assert [delta["id"] for delta in deltas] == [6]

        await service.flush_once()
        candle = await _read_candle(pair, "1m", NOW)
        assert candle["n"] == 6                    # aggregation never lost a trade
        assert candle["volume"] == Decimal("27.5")
    finally:
        await service.stop()


# ── ring gate + FX-safe codec ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_candles_and_tail_are_ready_gated_and_use_decimal_strings():
    pair = await _seed_pair()
    await _add_trade(pair, created_at=NOW + timedelta(seconds=10), price="1.25", input_amount="2")

    service = FxMarketDataService(async_session_maker)
    assert service.get_candles(pair, "1m", NOW, NOW + timedelta(minutes=1)) is None
    assert service.tail(pair, NOW) is None

    await service.start(write_owner=True)
    try:
        assert service.get_candles(pair, "5m", NOW, NOW + timedelta(minutes=1)) is None
        assert service.get_candles(pair, "1m", NOW - timedelta(days=30), NOW) is None
        rows = service.get_candles(pair, "1m", NOW, NOW + timedelta(minutes=1))
        assert rows is not None and len(rows) == 1
        assert rows[0]["interval"] == "1m"
        assert Decimal(rows[0]["open_price"]) == Decimal("1.25")

        tail = service.tail(pair, NOW + timedelta(seconds=30))
        assert tail["history_version"]
        assert tail["history_ready"] is True
        assert tail["history_tail_through_trade_id"] == 1
        assert tail["history_tail_at"] == (NOW + timedelta(seconds=30)).isoformat()
        for interval in ("10s", "1m", "15m", "1h"):
            segment = tail["history_tail"][interval]
            assert segment["n_buckets"] > 0
            assert (
                len(segment["t"]) == len(segment["o"]) == len(segment["h"])
                == len(segment["l"]) == len(segment["c"])
                == len(segment["v"]) == len(segment["trades"])
            )
        one_min = tail["history_tail"]["1m"]
        # The LMSR codec emits ints (float * 1e8); FX must emit decimal strings.
        assert isinstance(one_min["o"][0], str)
        assert Decimal(one_min["o"][0]) == Decimal("1.25")
        assert isinstance(one_min["v"][0], str)
        assert Decimal(one_min["v"][0]) == Decimal("2")
        assert one_min["trades"] == [1]
    finally:
        await service.stop()
