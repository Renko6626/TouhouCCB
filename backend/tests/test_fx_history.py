"""Task 4 FX materialised-read tests: /history/fx, /chart and rolling 24h.

Everything runs against real SQLite rows and the real read helpers; the runtime
singleton is reset around each test so the ring is empty and the persisted-candle
defence is exercised (which is exactly the read-only-instance path).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from app.core.database import async_session_maker
from app.models.fx import FxCandle, FxMarketDataState, FxPair, FxTrade
from app.services.fx.market_reads import (
    clear_fx_history_cache,
    fx_volume_24h,
    read_fx_chart,
    read_fx_snapshot_metadata,
)
from app.services.fx.market_state import FX_MARKET_DATA

UTC = timezone.utc


@pytest_asyncio.fixture(autouse=True)
async def _clean_runtime():
    FX_MARKET_DATA.reset()
    clear_fx_history_cache()
    yield
    FX_MARKET_DATA.reset()
    clear_fx_history_cache()


async def _seed_pair() -> int:
    async with async_session_maker() as s:
        pair = FxPair(
            currency_code=f"T{uuid4().hex[:8]}", currency_name="Test",
            status="trading", gold_reserve=Decimal("100"), foreign_reserve=Decimal("100"),
            buy_fee_rate=Decimal("0.01"), sell_fee_rate=Decimal("0.01"),
        )
        s.add(pair)
        await s.flush()
        pid = int(pair.id)
        await s.commit()
        return pid


async def _seed_state(pid: int, *, ready: bool, cursor: int, version: str = "gen-1") -> str:
    async with async_session_maker() as s:
        s.add(FxMarketDataState(pair_id=pid, last_trade_id=cursor,
                                history_version=version, history_ready=ready))
        await s.commit()
    return version


async def _seed_candle(pid: int, interval: str, bucket: datetime, *,
                       o="1.10", h="1.20", low="1.05", c="1.15", v="2", n=1) -> None:
    async with async_session_maker() as s:
        s.add(FxCandle(
            pair_id=pid, interval=interval, bucket_start=bucket,
            open_price=Decimal(o), high_price=Decimal(h), low_price=Decimal(low),
            close_price=Decimal(c), gold_volume=Decimal(v), n_trades=n,
            first_trade_at=bucket, first_trade_id=1,
            last_trade_at=bucket, last_trade_id=1,
        ))
        await s.commit()


async def _seed_trade(pid: int, trade_id: int, created_at: datetime, *,
                      side="buy", volume="1", post_price="1.10") -> None:
    amount = Decimal(volume)
    async with async_session_maker() as s:
        s.add(FxTrade(
            id=trade_id, pair_id=pid, side=side,
            input_amount=amount, output_amount=amount,
            pre_gold_reserve=Decimal("100"), pre_foreign_reserve=Decimal("100"),
            post_gold_reserve=Decimal("100"), post_foreign_reserve=Decimal("100"),
            post_price=Decimal(post_price), created_at=created_at,
        ))
        await s.commit()


def _previous_segment(seconds: int) -> int:
    now = int(datetime.now(UTC).timestamp())
    return now - (now % seconds) - seconds


# ── /history/fx/... ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_sealed_segment_is_immutable_200_with_decimal_strings(client):
    pid = await _seed_pair()
    version = await _seed_state(pid, ready=True, cursor=5)
    seg = _previous_segment(3600)
    await _seed_candle(pid, "1m", datetime.fromtimestamp(seg + 60, tz=UTC), c="1.23")

    resp = await client.get(f"/history/fx/{pid}/{version}/1m/{seg}.json")
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
    body = resp.json()
    assert body["t0"] == seg and body["step"] == 60 and body["n_buckets"] == 60
    idx = body["t"].index(1)
    assert body["c"][idx] == "1.23000000"
    assert body["v"][idx] == "2.000000"


@pytest.mark.asyncio
async def test_inflight_segment_is_404(client):
    pid = await _seed_pair()
    version = await _seed_state(pid, ready=True, cursor=0)
    now = int(datetime.now(UTC).timestamp())
    current = now - (now % 3600)
    resp = await client.get(f"/history/fx/{pid}/{version}/1m/{current}.json")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unknown_interval_and_unaligned_segment_are_404(client):
    pid = await _seed_pair()
    version = await _seed_state(pid, ready=True, cursor=0)
    seg = _previous_segment(3600)
    assert (await client.get(f"/history/fx/{pid}/{version}/5m/{seg}.json")).status_code == 404
    assert (await client.get(f"/history/fx/{pid}/{version}/1m/{seg + 61}.json")).status_code == 404


@pytest.mark.asyncio
async def test_not_ready_and_unknown_generation_are_non_200(client):
    pid = await _seed_pair()
    version = await _seed_state(pid, ready=False, cursor=0)
    seg = _previous_segment(3600)
    assert (await client.get(f"/history/fx/{pid}/{version}/1m/{seg}.json")).status_code == 503

    # Same pair, but the URL names a generation that is not current.
    async with async_session_maker() as s:
        row = await s.get(FxMarketDataState, pid)
        row.history_ready = True
        row.history_version = "gen-2"
        s.add(row)
        await s.commit()
    assert (await client.get(f"/history/fx/{pid}/{version}/1m/{seg}.json")).status_code == 404


@pytest.mark.asyncio
async def test_committed_trade_beyond_durable_cursor_blocks_immutable_200(client):
    pid = await _seed_pair()
    version = await _seed_state(pid, ready=True, cursor=2)
    seg = _previous_segment(3600)
    await _seed_candle(pid, "1m", datetime.fromtimestamp(seg + 60, tz=UTC))
    await _seed_trade(pid, 3, datetime.fromtimestamp(seg + 120, tz=UTC), volume="4")

    resp = await client.get(f"/history/fx/{pid}/{version}/1m/{seg}.json")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_committed_trade_outside_the_segment_does_not_block_200(client):
    pid = await _seed_pair()
    version = await _seed_state(pid, ready=True, cursor=2)
    seg = _previous_segment(3600)
    await _seed_candle(pid, "1m", datetime.fromtimestamp(seg + 60, tz=UTC))
    # A newer committed trade that is *not* in the closed segment must not block
    # caching the segment (the EXISTS check is segment-scoped, not a full scan).
    await _seed_trade(pid, 3, datetime.now(UTC), volume="4")

    resp = await client.get(f"/history/fx/{pid}/{version}/1m/{seg}.json")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_pending_fx_flush_blocks_immutable_200(client):
    pid = await _seed_pair()
    version = await _seed_state(pid, ready=True, cursor=2)
    seg = _previous_segment(3600)
    await _seed_candle(pid, "1m", datetime.fromtimestamp(seg + 60, tz=UTC))
    pending_bucket = datetime.fromtimestamp(seg + 120, tz=UTC)
    FX_MARKET_DATA.flusher.add_batch(
        pid, 2, 3,
        [{"pair_id": pid, "interval": "1m", "bucket_start": pending_bucket,
          "open_price": Decimal("1.1"), "high_price": Decimal("1.1"),
          "low_price": Decimal("1.1"), "close_price": Decimal("1.1"),
          "gold_volume": Decimal("1"), "n_trades": 1,
          "first_trade_at": pending_bucket, "first_trade_id": 3,
          "last_trade_at": pending_bucket, "last_trade_id": 3}],
        version,
    )
    resp = await client.get(f"/history/fx/{pid}/{version}/1m/{seg}.json")
    assert resp.status_code == 503


# ── /chart ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_chart_returns_materialised_candles_and_503s_when_not_ready(client):
    pid = await _seed_pair()
    version = await _seed_state(pid, ready=True, cursor=0)
    seg = _previous_segment(3600)
    await _seed_candle(pid, "1m", datetime.fromtimestamp(seg + 60, tz=UTC), c="1.99")
    start = datetime.fromtimestamp(seg, tz=UTC)
    resp = await client.get(
        f"/api/v1/fx/pairs/{pid}/chart",
        params={"interval": "1m", "from": start.isoformat(),
                "to": (start + timedelta(hours=1)).isoformat()},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-fx-history-version"] == version
    assert resp.headers["x-fx-through-trade-id"] == "0"
    candles = resp.json()
    assert len(candles) == 1
    assert set(candles[0]) == {"bucket_start", "interval", "open", "high", "low", "close", "volume"}
    assert candles[0]["close"] == 1.99

    pid2 = await _seed_pair()
    await _seed_state(pid2, ready=False, cursor=0)
    resp2 = await client.get(
        f"/api/v1/fx/pairs/{pid2}/chart",
        params={"interval": "1m", "from": start.isoformat(),
                "to": (start + timedelta(hours=1)).isoformat()},
    )
    assert resp2.status_code == 503
    # A refusal must never carry coverage metadata.
    assert "x-fx-through-trade-id" not in resp2.headers


@pytest.mark.asyncio
async def test_chart_coverage_headers_describe_body_after_midread_flush(client, monkeypatch):
    """A flush committing mid-request must not leave an old cursor on newer rows.

    The candle+state statement is captured first (durable cursor 2), then the
    injected flush writes the id-3 candle and moves the cursor to 3 *before* the
    tail is read.  The response header must describe the final body: through 3
    and the id-3 volume counted exactly once.
    """
    pid = await _seed_pair()
    version = await _seed_state(pid, ready=True, cursor=2)
    start = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    await _seed_candle(pid, "1m", start, v="2", n=2)
    await _seed_trade(pid, 3, start + timedelta(seconds=20), volume="3")

    from app.services.fx import market_reads
    original = market_reads._load_candles_with_state
    fired = {"done": False}

    def _snapshot(row):
        return SimpleNamespace(
            pair_id=int(row.pair_id), interval=row.interval,
            bucket_start=row.bucket_start, open_price=row.open_price,
            high_price=row.high_price, low_price=row.low_price,
            close_price=row.close_price, gold_volume=row.gold_volume,
            n_trades=int(row.n_trades), first_trade_at=row.first_trade_at,
            first_trade_id=int(row.first_trade_id), last_trade_at=row.last_trade_at,
            last_trade_id=int(row.last_trade_id),
        )

    async def interleaved(db, pair_id, interval, start_, end_):
        rows, st = await original(db, pair_id, interval, start_, end_)
        if not fired["done"] and st is not None and int(st.last_trade_id) == 2:
            fired["done"] = True
            snap_rows = [_snapshot(row) for row in rows]
            snap_state = SimpleNamespace(
                pair_id=int(st.pair_id), last_trade_id=int(st.last_trade_id),
                history_version=str(st.history_version),
                history_ready=bool(st.history_ready),
            )
            # Commit the tail trade exactly as the flusher would, on the same
            # connection, and expire the request's ORM objects.
            candle = await db.get(FxCandle, (pid, interval, start_))
            candle.gold_volume = candle.gold_volume + Decimal("3")
            candle.n_trades = candle.n_trades + 1
            state_row = await db.get(FxMarketDataState, pid)
            state_row.last_trade_id = 3
            await db.commit()
            return snap_rows, snap_state
        return rows, st

    monkeypatch.setattr(market_reads, "_load_candles_with_state", interleaved)

    resp = await client.get(
        f"/api/v1/fx/pairs/{pid}/chart",
        params={"interval": "1m", "from": start.isoformat(),
                "to": (start + timedelta(minutes=1)).isoformat()},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-fx-history-version"] == version
    assert resp.headers["x-fx-through-trade-id"] == "3"
    body = resp.json()
    assert len(body) == 1
    assert Decimal(str(body[0]["volume"])) == Decimal("5")


@pytest.mark.asyncio
async def test_chart_refuses_when_state_has_no_ready_row(client):
    """A pair without a persisted state is a 503, not fabricated coverage."""
    pid = await _seed_pair()
    start = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    resp = await client.get(
        f"/api/v1/fx/pairs/{pid}/chart",
        params={"interval": "1m", "from": start.isoformat(),
                "to": (start + timedelta(minutes=1)).isoformat()},
    )
    assert resp.status_code == 503
    assert "x-fx-history-version" not in resp.headers


@pytest.mark.asyncio
async def test_chart_merges_unflushed_tail_without_double_counting():
    """Persisted candle through ``durable`` + projected trades after it add once."""
    pid = await _seed_pair()
    await _seed_state(pid, ready=True, cursor=2)
    start = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    await _seed_candle(pid, "1m", start, v="2", n=2)
    await _seed_trade(pid, 3, start + timedelta(seconds=20), volume="3")
    await _seed_trade(pid, 4, start + timedelta(seconds=40), volume="4")

    async with async_session_maker() as db:
        rows = await read_fx_chart(db, pid, "1m", start, start + timedelta(minutes=1))

    assert len(rows) == 1
    assert rows[0]["volume"] == Decimal("9")
    # The same tail trades are projected into whichever interval is requested
    # (only 1m has a durable base row here, so 1h is the raw tail volume).
    async with async_session_maker() as db:
        hourly = await read_fx_chart(db, pid, "1h", start, start + timedelta(hours=1))
    assert len(hourly) == 1
    assert hourly[0]["volume"] == Decimal("7")


# ── rolling 24h volume ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_fx_volume_24h_is_none_until_ready():
    pid = await _seed_pair()
    await _seed_state(pid, ready=False, cursor=0)
    async with async_session_maker() as db:
        assert await fx_volume_24h(db, pid, datetime(2026, 10, 1, 12, 0, 30, tzinfo=UTC)) is None


@pytest.mark.asyncio
async def test_fx_volume_24h_sums_candles_boundaries_and_outstanding_tail():
    pid = await _seed_pair()
    await _seed_state(pid, ready=True, cursor=2)
    now = datetime(2026, 10, 1, 12, 0, 30, tzinfo=UTC)
    since = now - timedelta(hours=24)                       # 09-30 12:00:30
    full_start = datetime(2026, 9, 30, 12, 1, tzinfo=UTC)   # ceil(since, 1m)
    # Full-minute candle (through the durable cursor) inside the window.
    await _seed_candle(pid, "1m", full_start, v="5", n=3)
    # Partial head: covered by the raw boundary sum.
    await _seed_trade(pid, 1, datetime(2026, 9, 30, 12, 0, 40, tzinfo=UTC), volume="0.5")
    # Partial tail: covered by the raw boundary sum.
    await _seed_trade(pid, 2, datetime(2026, 10, 1, 12, 0, 10, tzinfo=UTC), volume="0.25")
    # Committed past the durable cursor but still inside a full minute: tail sum.
    await _seed_trade(pid, 3, datetime(2026, 9, 30, 12, 1, 20, tzinfo=UTC), volume="1.5")
    # Outside the rolling window: excluded.
    await _seed_trade(pid, 4, since - timedelta(seconds=1), volume="100")

    async with async_session_maker() as db:
        total = await fx_volume_24h(db, pid, now)
    assert total == Decimal("7.25")


@pytest.mark.asyncio
async def test_fx_volume_24h_ready_empty_inactive_pair_is_zero():
    """A ready but trade-less (and paused) pair is exactly 0, not an error."""
    pid = await _seed_pair()
    await _seed_state(pid, ready=True, cursor=0)
    async with async_session_maker() as s:
        pair = await s.get(FxPair, pid)
        pair.status = "paused"
        s.add(pair)
        await s.commit()

    async with async_session_maker() as db:
        total = await fx_volume_24h(db, pid, datetime(2026, 10, 1, 12, 0, 30, tzinfo=UTC))
    assert total == Decimal("0")


# ── public snapshot metadata (homepage bootstrap) ───────────────────────────
@pytest.mark.asyncio
async def test_read_fx_snapshot_metadata_public_bootstrap_fields():
    pid = await _seed_pair()
    when = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    async with async_session_maker() as db:
        meta = await read_fx_snapshot_metadata(db, pid, when)
    assert meta == {"history_version": None, "history_ready": False}

    version = await _seed_state(pid, ready=True, cursor=0)
    async with async_session_maker() as db:
        meta = await read_fx_snapshot_metadata(db, pid, when)
    assert meta == {"history_version": version, "history_ready": True}

    # Not-ready states still expose the opaque version but keep ready False, so
    # the frontend loader falls back instead of trusting a half-built history.
    pid2 = await _seed_pair()
    version2 = await _seed_state(pid2, ready=False, cursor=0)
    async with async_session_maker() as db:
        meta2 = await read_fx_snapshot_metadata(db, pid2, when)
    assert meta2 == {"history_version": version2, "history_ready": False}
