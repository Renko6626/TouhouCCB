"""FX candle storage: pure aggregation, durable cursor, readiness and retries.

These exercise the real SQL path against the configured SQLite test database:
the pure function is order-aware, the batch is atomic with its cursor, a
duplicate/retried batch must not double-count volume, readiness is only changed
explicitly, a stale history generation is rejected, and the flusher retains
separate ranges across an uncertain commit.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import async_session_maker, engine
from app.models.fx import FxCandle, FxMarketDataState, FxPair
from app.services.fx.candle_flusher import FxCandleFlusher
from app.services.fx.candles import (
    FxCandleCursorConflict,
    apply_candle_batch,
    compute_fx_candle_rows,
    load_market_data_state,
)

UTC = timezone.utc
BASE = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


class _LoseCommitAfterSuccessSession(AsyncSession):
    """Session that commits for real, then reports the commit as lost once.

    Models the uncertain-flush case: the durable checkpoint has advanced even
    though the caller saw a connection error.
    """

    lose_next = False

    def begin(self):
        real = super().begin()

        class _Ctx:
            async def __aenter__(self_):
                return await real.__aenter__()

            async def __aexit__(self_, *exc):
                result = await real.__aexit__(*exc)
                if exc[0] is None and _LoseCommitAfterSuccessSession.lose_next:
                    _LoseCommitAfterSuccessSession.lose_next = False
                    raise RuntimeError("connection lost after an unknown commit outcome")
                return result

        return _Ctx()


def _trade(tid, ts, price, *, side="buy", input_amount="2", output_amount="1", pair_id=1):
    return SimpleNamespace(
        id=tid, pair_id=pair_id, created_at=ts, post_price=Decimal(price),
        side=side, input_amount=Decimal(input_amount), output_amount=Decimal(output_amount),
    )


def _at(seconds):
    return BASE + timedelta(seconds=seconds)


async def _seed_pair() -> int:
    async with async_session_maker() as db:
        pair = FxPair(currency_code="TST", currency_name="Test")
        db.add(pair)
        await db.flush()
        pair_id = pair.id
        await db.commit()
    return pair_id


async def _read_candle(pair_id: int, interval: str, bucket_start: datetime):
    async with async_session_maker() as db:
        return (await db.execute(
            select(FxCandle).where(
                FxCandle.pair_id == pair_id,
                FxCandle.interval == interval,
                FxCandle.bucket_start == bucket_start,
            )
        )).scalars().first()


async def _read_state(pair_id: int):
    async with async_session_maker() as db:
        return (await db.execute(
            select(FxMarketDataState).where(FxMarketDataState.pair_id == pair_id)
        )).scalars().first()


# ── pure aggregation ──────────────────────────────────────────────────────
def test_compute_rows_orders_open_close_by_created_at_then_id_for_all_intervals():
    rows = compute_fx_candle_rows([
        _trade(3, _at(20), "1.30"),
        _trade(1, _at(5), "1.10"),
        _trade(2, _at(5), "1.20"),   # same second as id 1: id breaks the tie
    ])

    assert {row["interval"] for row in rows} == {"10s", "1m", "15m", "1h"}
    minute = next(row for row in rows if row["interval"] == "1m")
    assert minute["bucket_start"] == BASE
    assert minute["open_price"] == Decimal("1.10")   # id 1 at :05
    assert minute["close_price"] == Decimal("1.30")  # id 3 at :20
    assert minute["high_price"] == Decimal("1.30")
    assert minute["low_price"] == Decimal("1.10")
    assert minute["gold_volume"] == Decimal("6")     # three buys × input 2
    assert minute["n_trades"] == 3
    assert (minute["first_trade_id"], minute["last_trade_id"]) == (1, 3)


def test_compute_rows_sell_uses_output_for_gold_volume():
    row = compute_fx_candle_rows([
        _trade(1, _at(5), "1.10", side="sell", input_amount="8", output_amount="3.25"),
    ])[0]
    assert row["gold_volume"] == Decimal("3.25")


def test_compute_rows_accepts_projected_mapping_fields():
    row = compute_fx_candle_rows([{
        "id": 7, "pair_id": 1, "created_at": _at(3), "post_price": Decimal("1.42"),
        "side": "sell", "input_amount": Decimal("9"), "output_amount": Decimal("2.5"),
    }])[0]
    assert row["close_price"] == Decimal("1.42")
    assert row["gold_volume"] == Decimal("2.5")


def test_compute_rows_bucket_utc_and_split_10s_vs_1m():
    rows = compute_fx_candle_rows([_trade(1, _at(5), "1.10"), _trade(2, _at(20), "1.20")])
    ten = [row for row in rows if row["interval"] == "10s"]
    one_min = [row for row in rows if row["interval"] == "1m"]
    assert len(ten) == 2                                     # :00 and :10 buckets
    assert len(one_min) == 1
    assert one_min[0]["gold_volume"] == Decimal("4")
    for row in rows:
        assert row["bucket_start"].tzinfo is not None
        assert row["bucket_start"].utcoffset() == timedelta(0)


# ── durable batch ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_apply_batch_persists_candles_cursor_and_version_atomically():
    pair_id = await _seed_pair()
    rows = compute_fx_candle_rows([_trade(1, _at(5), "1.10"), _trade(2, _at(20), "1.30")])

    async with async_session_maker() as db:
        async with db.begin():
            applied = await apply_candle_batch(
                db, pair_id=pair_id, expected_trade_id=0, through_trade_id=2,
                rows=rows, history_version="gen-1", history_ready=True,
            )

    assert applied is True
    candle = await _read_candle(pair_id, "1m", BASE)
    assert candle is not None
    assert candle.open_price == Decimal("1.10")
    assert candle.close_price == Decimal("1.30")
    assert candle.gold_volume == Decimal("4")
    state = await _read_state(pair_id)
    assert state.last_trade_id == 2
    assert state.history_version == "gen-1"
    assert state.history_ready is True


@pytest.mark.asyncio
async def test_apply_batch_duplicate_through_is_noop_without_double_count():
    pair_id = await _seed_pair()
    rows = compute_fx_candle_rows([_trade(1, _at(5), "1.10"), _trade(2, _at(20), "1.30")])

    async with async_session_maker() as db:
        async with db.begin():
            await apply_candle_batch(
                db, pair_id=pair_id, expected_trade_id=0, through_trade_id=2,
                rows=rows, history_version="gen-1",
            )
    async with async_session_maker() as db:
        async with db.begin():
            applied = await apply_candle_batch(
                db, pair_id=pair_id, expected_trade_id=0, through_trade_id=2,
                rows=rows, history_version="gen-1",
            )

    assert applied is False
    candle = await _read_candle(pair_id, "1m", BASE)
    assert candle.gold_volume == Decimal("4")
    assert candle.n_trades == 2
    assert (await _read_state(pair_id)).history_ready is False


@pytest.mark.asyncio
async def test_apply_batch_metadata_only_transitions_readiness():
    pair_id = await _seed_pair()
    rows = compute_fx_candle_rows([_trade(1, _at(5), "1.10")])
    async with async_session_maker() as db:
        async with db.begin():
            await apply_candle_batch(
                db, pair_id=pair_id, expected_trade_id=0, through_trade_id=1,
                rows=rows, history_version="gen-1",
            )
    assert (await _read_state(pair_id)).history_ready is False

    # Empty batch, same cursor: the explicit completion signal.
    async with async_session_maker() as db:
        async with db.begin():
            changed = await apply_candle_batch(
                db, pair_id=pair_id, expected_trade_id=1, through_trade_id=1,
                rows=[], history_version="gen-1", history_ready=True,
            )
    assert changed is True
    assert (await _read_state(pair_id)).history_ready is True

    async with async_session_maker() as db:
        async with db.begin():
            changed = await apply_candle_batch(
                db, pair_id=pair_id, expected_trade_id=1, through_trade_id=1,
                rows=[], history_version="gen-1", history_ready=True,
            )
    assert changed is False


@pytest.mark.asyncio
async def test_first_creation_receives_requested_generation_and_readiness():
    pair_id = await _seed_pair()
    async with async_session_maker() as db:
        async with db.begin():
            applied = await apply_candle_batch(
                db, pair_id=pair_id, expected_trade_id=0, through_trade_id=0,
                rows=[], history_version="gen-first", history_ready=True,
            )
    assert applied is True
    state = await _read_state(pair_id)
    assert state.history_version == "gen-first"
    assert state.history_ready is True


@pytest.mark.asyncio
async def test_apply_batch_rejects_stale_generation_even_at_same_cursor():
    pair_id = await _seed_pair()
    rows = compute_fx_candle_rows([_trade(1, _at(5), "1.10")])
    async with async_session_maker() as db:
        async with db.begin():
            await apply_candle_batch(
                db, pair_id=pair_id, expected_trade_id=0, through_trade_id=1,
                rows=rows, history_version="gen-1", history_ready=True,
            )

    for expected, through, batch_rows, ready in (
        (0, 1, rows, True),          # exact duplicate, stale generation
        (1, 1, [], False),           # metadata-only, stale generation
        (1, 2, rows, True),          # new range, stale generation
    ):
        async with async_session_maker() as db:
            with pytest.raises(FxCandleCursorConflict):
                async with db.begin():
                    await apply_candle_batch(
                        db, pair_id=pair_id, expected_trade_id=expected,
                        through_trade_id=through, rows=batch_rows,
                        history_version="gen-2", history_ready=ready,
                    )

    state = await _read_state(pair_id)
    assert state.history_version == "gen-1"     # generation never silently changed
    assert state.last_trade_id == 1
    assert (await _read_candle(pair_id, "1m", BASE)).gold_volume == Decimal("2")


@pytest.mark.asyncio
async def test_apply_batch_cursor_mismatch_raises_and_keeps_state():
    pair_id = await _seed_pair()
    rows = compute_fx_candle_rows([_trade(1, _at(5), "1.10"), _trade(2, _at(20), "1.30")])
    async with async_session_maker() as db:
        async with db.begin():
            await apply_candle_batch(
                db, pair_id=pair_id, expected_trade_id=0, through_trade_id=2,
                rows=rows, history_version="gen-1",
            )

    async with async_session_maker() as db:
        with pytest.raises(FxCandleCursorConflict):
            async with db.begin():
                await apply_candle_batch(
                    db, pair_id=pair_id, expected_trade_id=0, through_trade_id=3,
                    rows=rows, history_version="gen-1",
                )

    state = await _read_state(pair_id)
    assert state.last_trade_id == 2
    candle = await _read_candle(pair_id, "1m", BASE)
    assert candle.gold_volume == Decimal("4")


@pytest.mark.asyncio
async def test_apply_batch_out_of_order_batches_fix_open_and_close():
    pair_id = await _seed_pair()
    later = compute_fx_candle_rows([_trade(2, _at(20), "1.30"), _trade(3, _at(30), "1.40")])
    async with async_session_maker() as db:
        async with db.begin():
            await apply_candle_batch(
                db, pair_id=pair_id, expected_trade_id=0, through_trade_id=3,
                rows=later, history_version="gen-1",
            )

    earlier_and_more = compute_fx_candle_rows([_trade(1, _at(5), "1.10"), _trade(4, _at(40), "1.50")])
    async with async_session_maker() as db:
        async with db.begin():
            await apply_candle_batch(
                db, pair_id=pair_id, expected_trade_id=3, through_trade_id=4,
                rows=earlier_and_more, history_version="gen-1",
            )

    candle = await _read_candle(pair_id, "1m", BASE)
    assert candle.open_price == Decimal("1.10")
    assert candle.close_price == Decimal("1.50")
    assert candle.high_price == Decimal("1.50")
    assert candle.low_price == Decimal("1.10")
    assert candle.gold_volume == Decimal("8")
    assert candle.n_trades == 4
    assert (candle.first_trade_id, candle.last_trade_id) == (1, 4)


@pytest.mark.asyncio
async def test_load_market_data_state_is_not_committed_by_itself():
    pair_id = await _seed_pair()
    async with async_session_maker() as db:
        state = await load_market_data_state(db, pair_id)
        assert state.last_trade_id == 0
        assert state.history_ready is False
        assert state.history_version
        await db.rollback()

    assert await _read_state(pair_id) is None


# ── flusher ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_flusher_flushes_pending_batch_and_reports_watermark():
    pair_id = await _seed_pair()
    rows = compute_fx_candle_rows([_trade(1, _at(5), "1.10")])
    flusher = FxCandleFlusher(async_session_maker)
    flusher.add_batch(pair_id, 0, 1, rows, "gen-1")

    assert flusher.pending_count() == 1
    assert flusher.pending_watermark(pair_id) == 0
    assert flusher.pending_through(pair_id) == 1
    assert flusher.oldest_pending_bucket(pair_id) == BASE

    assert await flusher.flush_once() == len(rows)
    assert flusher.pending_count() == 0
    assert flusher.pending_watermark(pair_id) is None
    assert (await _read_state(pair_id)).last_trade_id == 1
    assert (await _read_candle(pair_id, "1m", BASE)).gold_volume == Decimal("2")


@pytest.mark.asyncio
async def test_flusher_retry_of_committed_batch_does_not_double_count():
    pair_id = await _seed_pair()
    rows = compute_fx_candle_rows([_trade(1, _at(5), "1.10"), _trade(2, _at(20), "1.30")])
    first = FxCandleFlusher(async_session_maker)
    first.add_batch(pair_id, 0, 2, rows, "gen-1")
    assert await first.flush_once() == len(rows)

    # A restart retries the same (already durable) batch.
    retry = FxCandleFlusher(async_session_maker)
    retry.add_batch(pair_id, 0, 2, rows, "gen-1")
    assert await retry.flush_once() == 0
    assert retry.pending_count() == 0

    candle = await _read_candle(pair_id, "1m", BASE)
    assert candle.gold_volume == Decimal("4")
    assert candle.n_trades == 2


@pytest.mark.asyncio
async def test_flusher_keeps_readiness_false_until_explicit_completion():
    pair_id = await _seed_pair()
    rows = compute_fx_candle_rows([_trade(1, _at(5), "1.10")])
    flusher = FxCandleFlusher(async_session_maker)
    flusher.add_batch(pair_id, 0, 1, rows, "gen-1")          # history_ready omitted
    assert await flusher.flush_once() == len(rows)
    assert (await _read_state(pair_id)).history_ready is False

    flusher.add_batch(pair_id, 1, 1, [], "gen-1", history_ready=True)
    assert await flusher.flush_once() == 0
    assert (await _read_state(pair_id)).history_ready is True
    assert flusher.pending_count() == 0


@pytest.mark.asyncio
async def test_flusher_unknown_commit_then_new_range_retains_and_avoids_double_count():
    """Real-SQL regression for the uncertain-commit + newly queued batch hazard.

    The first flush commits range (0, 1] but reports it as lost.  A newly queued
    (1, 2] range must be retained separately rather than merged into (0, 2]:
    the durable cursor is already 1, so a combined batch claiming expected 0
    would conflict forever or risk re-adding the committed prefix.
    """
    pair_id = await _seed_pair()
    rows_a = compute_fx_candle_rows([_trade(1, _at(5), "1.10", input_amount="2")])
    rows_b = compute_fx_candle_rows([_trade(2, _at(6), "1.20", input_amount="3")])
    maker = async_sessionmaker(
        engine, class_=_LoseCommitAfterSuccessSession, expire_on_commit=False,
    )
    flusher = FxCandleFlusher(maker)
    _LoseCommitAfterSuccessSession.lose_next = False
    flusher.add_batch(pair_id, 0, 1, rows_a, "gen-1")

    _LoseCommitAfterSuccessSession.lose_next = True
    assert await flusher.flush_once() == 0                 # committed, outcome unknown
    assert (await _read_state(pair_id)).last_trade_id == 1  # prefix is really durable
    assert flusher.pending_ranges(pair_id) == 1

    # The naive widening the old flusher produced is exactly what must not work.
    combined = compute_fx_candle_rows([
        _trade(1, _at(5), "1.10", input_amount="2"),
        _trade(2, _at(6), "1.20", input_amount="3"),
    ])
    async with async_session_maker() as db:
        with pytest.raises(FxCandleCursorConflict):
            async with db.begin():
                await apply_candle_batch(
                    db, pair_id=pair_id, expected_trade_id=0, through_trade_id=2,
                    rows=combined, history_version="gen-1",
                )

    flusher.add_batch(pair_id, 1, 2, rows_b, "gen-1")       # retained separately
    assert flusher.pending_ranges(pair_id) == 2
    assert flusher.pending_watermark(pair_id) == 0
    assert flusher.pending_through(pair_id) == 2

    assert await flusher.flush_once() == len(rows_b)        # (0,1] was a no-op retry
    assert flusher.pending_count() == 0
    assert (await _read_state(pair_id)).last_trade_id == 2
    candle = await _read_candle(pair_id, "1m", BASE)
    assert candle.gold_volume == Decimal("5")               # 2 + 3 exactly once
    assert candle.n_trades == 2


@pytest.mark.asyncio
async def test_flusher_uncertain_range_refuses_widening_recompute():
    pair_id = await _seed_pair()
    flusher = FxCandleFlusher(async_session_maker)
    head = compute_fx_candle_rows([_trade(1, _at(5), "1.10")])
    flusher.add_batch(pair_id, 0, 1, head, "gen-1")

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    import app.services.fx.candle_flusher as flusher_module
    original = flusher_module.apply_candle_batch
    flusher_module.apply_candle_batch = boom
    try:
        assert await flusher.flush_once() == 0
    finally:
        flusher_module.apply_candle_batch = original

    wider = compute_fx_candle_rows([_trade(1, _at(5), "1.10"), _trade(2, _at(6), "1.20")])
    flusher.add_batch(pair_id, 0, 2, wider, "gen-1")
    assert flusher.pending_through(pair_id) == 1            # not widened
    assert await flusher.flush_once() == len(head)
    assert (await _read_candle(pair_id, "1m", BASE)).gold_volume == Decimal("2")


@pytest.mark.asyncio
async def test_flusher_rejects_mixed_generations():
    pair_id = await _seed_pair()
    rows = compute_fx_candle_rows([_trade(1, _at(5), "1.10")])
    flusher = FxCandleFlusher(async_session_maker)
    flusher.add_batch(pair_id, 0, 1, rows, "gen-1")
    with pytest.raises(FxCandleCursorConflict):
        flusher.add_batch(pair_id, 1, 2, rows, "gen-2")


@pytest.mark.asyncio
async def test_flusher_failure_keeps_batch_for_retry(monkeypatch):
    pair_id = await _seed_pair()
    rows = compute_fx_candle_rows([_trade(1, _at(5), "1.10")])
    flusher = FxCandleFlusher(async_session_maker)
    flusher.add_batch(pair_id, 0, 1, rows, "gen-1")

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.services.fx.candle_flusher.apply_candle_batch", boom)
    assert await flusher.flush_once() == 0
    assert flusher.pending_count() == 1
    assert flusher.pending_watermark(pair_id) == 0
    monkeypatch.undo()

    assert await flusher.flush_once() == len(rows)
    assert flusher.pending_count() == 0
    assert (await _read_candle(pair_id, "1m", BASE)).gold_volume == Decimal("2")


@pytest.mark.asyncio
async def test_flusher_merges_contiguous_batches_into_one_write():
    pair_id = await _seed_pair()
    first = compute_fx_candle_rows([_trade(1, _at(5), "1.10", input_amount="2")])
    second = compute_fx_candle_rows([_trade(2, _at(6), "1.20", input_amount="3")])
    flusher = FxCandleFlusher(async_session_maker)
    flusher.add_batch(pair_id, 0, 1, first, "gen-1")
    flusher.add_batch(pair_id, 1, 2, second, "gen-1")

    assert flusher.pending_count() == 1
    assert flusher.pending_watermark(pair_id) == 0
    assert flusher.pending_through(pair_id) == 2

    assert await flusher.flush_once() == len(first)
    candle = await _read_candle(pair_id, "1m", BASE)
    assert candle.gold_volume == Decimal("5")
    assert candle.open_price == Decimal("1.10")
    assert candle.close_price == Decimal("1.20")


@pytest.mark.asyncio
async def test_flusher_recomputed_overlap_replaces_instead_of_double_counting():
    pair_id = await _seed_pair()
    flusher = FxCandleFlusher(async_session_maker)
    flusher.add_batch(pair_id, 0, 1, compute_fx_candle_rows([_trade(1, _at(5), "1.10", input_amount="2")]), "gen-1")
    # Catch-up before the flush recomputes from the same start with a wider range.
    recomputed = compute_fx_candle_rows([
        _trade(1, _at(5), "1.10", input_amount="2"),
        _trade(2, _at(6), "1.20", input_amount="3"),
    ])
    flusher.add_batch(pair_id, 0, 2, recomputed, "gen-1")

    assert flusher.pending_through(pair_id) == 2
    assert await flusher.flush_once() == len(recomputed)
    candle = await _read_candle(pair_id, "1m", BASE)
    assert candle.gold_volume == Decimal("5")     # trade 1 counted once, not twice
    assert candle.n_trades == 2


@pytest.mark.asyncio
async def test_flusher_rejects_non_contiguous_batch():
    pair_id = await _seed_pair()
    rows = compute_fx_candle_rows([_trade(1, _at(5), "1.10")])
    flusher = FxCandleFlusher(async_session_maker)
    flusher.add_batch(pair_id, 0, 1, rows, "gen-1")
    with pytest.raises(FxCandleCursorConflict):
        flusher.add_batch(pair_id, 5, 6, rows, "gen-1")
