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
from sqlalchemy import delete, func, select

from app.core.database import async_session_maker
from app.models.audit import AuditEvent
from app.services.fx.pool_events import append_pool_event
from app.models.fx import FxCandle, FxMarketDataState, FxPair, FxPoolEvent, FxTrade
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
        audit = AuditEvent(event_type="fx_fund", ref_table="fx_pair", ref_id=pair_id,
                           ts=NOW, payload={"action": "initial_issuance"})
        db.add(audit)
        await db.flush()
        append_pool_event(db, pair_id=pair_id, kind="initialize", created_at=NOW,
                          pre_reserves=None, post_reserves=(Decimal("1000"), Decimal("1000")),
                          gold_volume=Decimal("0"), audit_id=audit.id)
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
        event = append_pool_event(db, pair_id=pair_id, kind="trade", created_at=created_at,
                          pre_reserves=(trade.pre_gold_reserve, trade.pre_foreign_reserve),
                          post_reserves=(Decimal(price) * Decimal("1000"), Decimal("1000")),
                          gold_volume=Decimal(input_amount if side == "buy" else output_amount),
                          trade_id=trade.id)
        await db.flush()
        event_id = int(event.id)
        await db.commit()
    return event_id


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
            "first_id": row.first_event_id,
            "last_id": row.last_event_id,
        }


async def _read_state(pair_id: int) -> dict | None:
    async with async_session_maker() as db:
        row = (await db.execute(
            select(FxMarketDataState).where(FxMarketDataState.pair_id == pair_id)
        )).scalars().first()
        if row is None:
            return None
        return {
            "last_event_id": int(row.last_event_id),
            "history_version": str(row.history_version),
            "history_ready": bool(row.history_ready),
        }


async def _count(model) -> int:
    async with async_session_maker() as db:
        return int((await db.execute(select(func.count()).select_from(model))).scalar() or 0)


async def _rebuild_pair(pair_id: int, *, through_event_id: int, new_version: str) -> None:
    """Simulate an out-of-band rebuild: derived rows replaced under a new gen."""
    async with async_session_maker() as db:
        async with db.begin():
            await db.execute(delete(FxCandle).where(FxCandle.pair_id == pair_id))
            await db.execute(delete(FxMarketDataState).where(FxMarketDataState.pair_id == pair_id))
        async with db.begin():
            projected = (await db.execute(
                select(
                    FxPoolEvent.id, FxPoolEvent.pair_id, FxPoolEvent.created_at, FxPoolEvent.kind,
                    FxPoolEvent.pre_gold_reserve, FxPoolEvent.pre_foreign_reserve,
                    FxPoolEvent.post_gold_reserve, FxPoolEvent.post_foreign_reserve, FxPoolEvent.gold_volume,
                ).where(FxPoolEvent.pair_id == pair_id, FxPoolEvent.id <= through_event_id)
            )).all()
            await apply_candle_batch(
                db, pair_id=pair_id, expected_event_id=0, through_event_id=through_event_id,
                rows=compute_fx_candle_rows(projected), history_version=new_version,
                history_ready=True,
            )


class _BarrierService(FxMarketDataService):
    """Test seam: park page fetches so two catch-ups are forced to overlap."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fetch_calls = 0
        self.first_fetch = asyncio.Event()
        self.release = asyncio.Event()

    async def _fetch_page(self, pair_id: int, after: int, through: int) -> list:
        rows = await super()._fetch_page(pair_id, after, through)
        if not any(row.kind == "trade" for row in rows):
            return rows
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
    assert (a1, b1, a2) == (3, 4, 5)  # interleaved, non-contiguous per pair

    service = FxMarketDataService(async_session_maker, batch_size=2)
    await service.start(write_owner=True)
    try:
        assert service.state(pair_a)["applied_event_id"] == a2  # pair max, not global max at start
        assert service.state(pair_b)["applied_event_id"] == b1
        candle = await _read_candle(pair_a, "1m", NOW)
        assert candle["open"] == Decimal("1.00")
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
        assert restarted.state(pair_a)["durable_event_id"] == a2
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
        assert candle["open"] == Decimal("1.00")   # id 2 at :10
        assert candle["close"] == Decimal("1.30")  # id 1 at :30
        assert candle["high"] == Decimal("1.30")
        assert candle["low"] == Decimal("1.00")
        assert candle["volume"] == Decimal("6")
        assert candle["n"] == 3
        assert (candle["first_id"], candle["last_id"]) == (1, 2)

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
        assert service.state(pair)["applied_event_id"] == 1
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
            if state is not None and state["applied_event_id"] >= trade_id:
                break
            if loop.time() > deadline:
                pytest.fail("background consumer never caught up after notify")
            await asyncio.sleep(0.02)
        assert service.state(pair)["history_ready"] is True
        assert (await _read_state(pair))["last_event_id"] == trade_id
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
        assert state["durable_event_id"] == 2
        rows = reader.get_candles(pair, "1m", NOW, NOW + timedelta(minutes=1))
        assert rows is not None and rows[0]["n_trades"] == 1
        assert await _count(FxMarketDataState) == state_before
        assert await _count(FxCandle) == candles_before
    finally:
        await reader.stop()


# ── one pair's rebuild must not drop another pair's unflushed range ────────
@pytest.mark.asyncio
async def test_resync_of_one_pair_preserves_other_pairs_pending_range(monkeypatch):
    pair_a = await _seed_pair("AAA")
    pair_b = await _seed_pair("BBB")
    a1 = await _add_trade(pair_a, created_at=NOW + timedelta(seconds=5), input_amount="2")
    b1 = await _add_trade(pair_b, created_at=NOW + timedelta(seconds=6), input_amount="3")

    service = FxMarketDataService(async_session_maker)
    await service.start(write_owner=True)
    try:
        a2 = await _add_trade(pair_a, created_at=NOW + timedelta(seconds=15), input_amount="5")
        b2 = await _add_trade(pair_b, created_at=NOW + timedelta(seconds=16), input_amount="7")
        await service.catch_up(pair_a)   # A queued, not yet flushed
        await service.catch_up(pair_b)   # B queued, not yet flushed
        assert service.state(pair_a)["durable_event_id"] == a1
        assert service.state(pair_b)["durable_event_id"] == b1

        # Out-of-band rebuild of B only: derived rows replaced under a new
        # generation, so B's queued (old-generation) range is now stale.
        await _rebuild_pair(pair_b, through_event_id=b1, new_version="rebuilt-gen-b")

        # First flush: A hits a transient failure (range retained uncertain),
        # B is rejected on the stale generation and triggers the per-pair
        # resync inside the post-flush checkpoint sync.  Resync must not touch
        # A's retained range.
        import app.services.fx.candle_flusher as flusher_module

        real_apply = flusher_module.apply_candle_batch

        async def _flaky_apply(db, *, pair_id, **kwargs):
            if pair_id == pair_a:
                raise RuntimeError("transient db error for A")
            return await real_apply(db, pair_id=pair_id, **kwargs)

        monkeypatch.setattr(flusher_module, "apply_candle_batch", _flaky_apply)
        await service.flush_once()
        monkeypatch.undo()

        # No false durability from "nothing pending": A really is only at a1.
        assert service.state(pair_a)["durable_event_id"] == a1
        assert service.state(pair_a)["applied_event_id"] == a2
        assert service.flusher.pending_ranges(pair_a) == 1

        state_b = service.state(pair_b)
        assert state_b["history_version"] == "rebuilt-gen-b"
        assert state_b["durable_event_id"] == b1           # rebuilt from checkpoint
        assert state_b["applied_event_id"] == b1           # ready to re-fetch b2

        # A's retained range flushes on the next attempt: preserved once.
        await service.flush_once()
        candle_a = await _read_candle(pair_a, "1m", NOW)
        assert candle_a["n"] == 2
        assert candle_a["volume"] == Decimal("7")          # 2 + 5, counted once
        assert service.state(pair_a)["durable_event_id"] == a2

        # Continued append on A after the resync still works.
        a3 = await _add_trade(pair_a, created_at=NOW + timedelta(seconds=25), input_amount="11")
        await service.catch_up(pair_a)
        await service.flush_once()
        candle_a = await _read_candle(pair_a, "1m", NOW)
        assert candle_a["n"] == 3
        assert candle_a["volume"] == Decimal("18")         # 2 + 5 + 11
        assert service.state(pair_a)["durable_event_id"] == a3

        # B's rejected trade is recovered exactly once from the checkpoint.
        await service.catch_up(pair_b)
        await service.flush_once()
        candle_b = await _read_candle(pair_b, "1m", NOW)
        assert candle_b["n"] == 2
        assert candle_b["volume"] == Decimal("10")         # 3 + 7, no double count
        assert service.state(pair_b)["durable_event_id"] == b2
    finally:
        await service.stop()


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
                    FxPoolEvent.id, FxPoolEvent.pair_id, FxPoolEvent.created_at, FxPoolEvent.kind,
                    FxPoolEvent.pre_gold_reserve, FxPoolEvent.pre_foreign_reserve,
                    FxPoolEvent.post_gold_reserve, FxPoolEvent.post_foreign_reserve, FxPoolEvent.gold_volume,
                ).where(FxPoolEvent.id <= t1)
            )).all()
            await apply_candle_batch(
                db, pair_id=pair, expected_event_id=0, through_event_id=t1,
                rows=compute_fx_candle_rows(projected), history_version="v1",
                history_ready=False,
            )

    service = FxMarketDataService(async_session_maker, batch_size=1)
    await service.start(write_owner=True)
    try:
        state = service.state(pair)
        assert state["history_ready"] is True        # flipped only after trade 2
        assert state["applied_event_id"] == t2
        candle = await _read_candle(pair, "1m", NOW)
        assert candle["volume"] == Decimal("5")      # trade 1 counted exactly once
        assert candle["n"] == 2
        assert (candle["first_id"], candle["last_id"]) == (1, t2)
        durable = await _read_state(pair)
        assert durable["history_ready"] is True
        assert durable["last_event_id"] == t2
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
        deltas, invalidated = service.drain_public_events(pair)
        assert invalidated is False
        assert [delta["id"] for delta in deltas] == [2, 3]
        assert Decimal(deltas[1]["gold_volume"]) == Decimal("3.5")  # sell uses gold-out
        assert Decimal(deltas[1]["post_price"]) == Decimal("1.20")
        assert deltas[1]["pre_price"] == "1.000000"
        assert deltas[0]["ts"].endswith("+00:00")
        assert isinstance(deltas[0]["post_price"], str)

        await _add_trade(pair, created_at=NOW + timedelta(seconds=3), input_amount="4")
        await _add_trade(pair, created_at=NOW + timedelta(seconds=4), input_amount="5")
        await _add_trade(pair, created_at=NOW + timedelta(seconds=5), input_amount="6")
        await service.catch_up(pair)
        deltas, invalidated = service.drain_public_events(pair)
        assert invalidated is True                 # partial buffer dropped
        assert deltas == []

        await _add_trade(pair, created_at=NOW + timedelta(seconds=6), input_amount="7")
        await service.catch_up(pair)
        deltas, invalidated = service.drain_public_events(pair)
        assert invalidated is False
        assert [delta["id"] for delta in deltas] == [7]

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
        assert Decimal(rows[0]["open_price"]) == Decimal("1.00")

        tail = service.tail(pair, NOW + timedelta(seconds=30))
        assert tail["history_version"]
        assert tail["history_ready"] is True
        assert tail["history_tail_through_event_id"] == 2
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
        assert Decimal(one_min["o"][0]) == Decimal("1.00")
        assert isinstance(one_min["v"][0], str)
        assert Decimal(one_min["v"][0]) == Decimal("2")
        assert one_min["trades"] == [1]
    finally:
        await service.stop()


async def _add_fund(pair_id: int, at: datetime, pre: str, post: str) -> int:
    async with async_session_maker() as db:
        audit = AuditEvent(event_type="fx_fund", ref_table="fx_pair", ref_id=pair_id, ts=at)
        db.add(audit)
        await db.flush()
        event = append_pool_event(db, pair_id=pair_id, kind="fund", created_at=at,
                                  pre_reserves=(Decimal(pre), Decimal("1")),
                                  post_reserves=(Decimal(post), Decimal("1")),
                                  gold_volume=Decimal("0"), audit_id=audit.id)
        await db.flush()
        event_id = event.id
        await db.commit()
        return event_id


@pytest.mark.asyncio
async def test_funding_only_and_mixed_bucket_preserve_event_ohlc_without_trade_volume():
    pair = await _seed_pair()
    fund_id = await _add_fund(pair, NOW + timedelta(seconds=5), "1", "1.5")
    service = FxMarketDataService(async_session_maker)
    await service.start(write_owner=True)
    try:
        candle = await _read_candle(pair, "1m", NOW)
        assert (candle["open"], candle["close"], candle["high"]) == (Decimal("1"), Decimal("1.5"), Decimal("1.5"))
        assert candle["volume"] == 0 and candle["n"] == 0
        trade = await _add_trade(pair, created_at=NOW + timedelta(seconds=6), price="1.2")
        await _add_fund(pair, NOW + timedelta(seconds=7), "1.2", "0.8")
        await _add_trade(pair, created_at=NOW + timedelta(seconds=8), price="1.1", input_amount="3")
        await service.catch_up(pair)
        await service.flush_once()
        candle = await _read_candle(pair, "1m", NOW)
        assert (candle["high"], candle["low"], candle["close"]) == (Decimal("1.5"), Decimal("0.8"), Decimal("1.1"))
        assert candle["volume"] == 5 and candle["n"] == 2
        segment = service.tail(pair, NOW + timedelta(seconds=9))["history_tail"]["1m"]
        assert segment["events"] == [5]  # initialize, two funds, two real trades
        assert service.state(pair)["durable_event_id"] > fund_id
        events, invalidated = service.drain_public_events(pair)
        assert not invalidated and events[0]["id"] == trade
        assert events[1]["gold_volume"] == "0.000000" and events[1]["n_trades"] == 0
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_catch_up_yields_after_ten_pages_and_requeues_remaining_events():
    pair = await _seed_pair()
    service = FxMarketDataService(async_session_maker, batch_size=1)
    await service.start(write_owner=True)
    await service._cancel_task()  # inspect one bounded round without background races
    try:
        for second in range(1, 13):
            last = await _add_fund(pair, NOW + timedelta(seconds=second), "1", "1")
        applied = await service.catch_up(pair)
        assert applied < last and applied == 11
        assert pair in service._dirty
        await service.flush_once()
        assert await service.catch_up(pair) == last
        await service.flush_once()
        segment = service.tail(pair, NOW + timedelta(seconds=15))["history_tail"]["1m"]
        assert segment["events"] == [13] and segment["trades"] == [0]
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_cursor", [None, 0, 7])
async def test_legacy_sources_without_events_never_become_ready(legacy_cursor):
    async with async_session_maker() as db:
        pair = FxPair(currency_code="OLD", currency_name="Legacy")
        db.add(pair)
        await db.flush()
        db.add(FxTrade(pair_id=pair.id, side="buy", input_amount=Decimal("2"),
                       output_amount=Decimal("1"), pre_gold_reserve=Decimal("1"),
                       pre_foreign_reserve=Decimal("1"), post_gold_reserve=Decimal("2"),
                       post_foreign_reserve=Decimal("1"), post_price=Decimal("2"), created_at=NOW))
        if legacy_cursor is not None:
            db.add(FxMarketDataState(pair_id=pair.id, last_trade_id=legacy_cursor,
                                    history_version="legacy", history_ready=False))
        await db.commit()
        pid = pair.id
    service = FxMarketDataService(async_session_maker)
    await service.start(write_owner=True)
    try:
        assert service.state(pid)["history_ready"] is False
        assert service.get_candles(pid, "1m", NOW, NOW + timedelta(minutes=1)) is None
        assert await _count(FxCandle) == 0
        state = await _read_state(pid)
        assert state is None or (not state["history_ready"] and state["last_event_id"] == 0)
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_source", ["trade", "fund"])
async def test_initialize_does_not_certify_legacy_sources_missing_event_records(missing_source):
    pair = await _seed_pair()
    async with async_session_maker() as db:
        if missing_source == "fund":
            db.add(AuditEvent(event_type="fx_fund", ref_table="fx_pair", ref_id=pair, ts=NOW))
        else:
            db.add(FxTrade(pair_id=pair, side="buy", input_amount=Decimal("2"),
                           output_amount=Decimal("1"), pre_gold_reserve=Decimal("1"),
                           pre_foreign_reserve=Decimal("1"), post_gold_reserve=Decimal("2"),
                           post_foreign_reserve=Decimal("1"), post_price=Decimal("2"), created_at=NOW))
        await db.commit()
    service = FxMarketDataService(async_session_maker)
    await service.start(write_owner=True)
    try:
        assert not service.state(pair)["history_ready"]
        assert await service.catch_up(pair) == 0
        assert await _count(FxCandle) == 0
    finally:
        await service.stop()
