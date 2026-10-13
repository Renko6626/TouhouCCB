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
from fastapi import HTTPException

from app.core.database import async_session_maker
from app.models.audit import AuditEvent
from app.services.fx.pool_events import append_pool_event
from app.models.fx import FxCandle, FxMarketDataState, FxPair, FxPoolEvent, FxTrade
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
        s.add(FxMarketDataState(pair_id=pid, last_event_id=cursor,
                                history_version=version, history_ready=ready))
        await s.commit()
    return version


async def _seed_candle(pid: int, interval: str, bucket: datetime, *,
                       o="1.10", h="1.20", low="1.05", c="1.15", v="2", n=1) -> None:
    async with async_session_maker() as s:
        s.add(FxCandle(
            pair_id=pid, interval=interval, bucket_start=bucket,
            open_price=Decimal(o), high_price=Decimal(h), low_price=Decimal(low),
            close_price=Decimal(c), gold_volume=Decimal(v), n_trades=n, n_events=n,
            first_event_at=bucket, first_event_id=1,
            last_event_at=bucket, last_event_id=1,
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
        await s.flush()
        append_pool_event(s, pair_id=pid, kind="trade", created_at=created_at,
                          pre_reserves=(Decimal("100"), Decimal("100")),
                          post_reserves=(Decimal(post_price) * Decimal("100"), Decimal("100")),
                          gold_volume=amount, trade_id=trade_id)
        # Explicit ids model a captured coverage prefix independently of trade ids.
        event = next(row for row in s.new if isinstance(row, FxPoolEvent))
        event.id = trade_id
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
          "first_event_at": pending_bucket, "first_event_id": 3,
          "last_event_at": pending_bucket, "last_event_id": 3}],
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
    assert resp.headers["x-fx-through-event-id"] == "0"
    candles = resp.json()
    assert len(candles) == 59
    assert set(candles[0]) == {"bucket_start", "interval", "open", "high", "low", "close", "volume", "n_trades", "n_events"}
    assert candles[0]["n_events"] == 1
    assert all(row["n_events"] == 0 for row in candles[1:])
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
    assert "x-fx-through-event-id" not in resp2.headers


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
            n_trades=int(row.n_trades), n_events=int(row.n_events), first_event_at=row.first_event_at,
            first_event_id=int(row.first_event_id), last_event_at=row.last_event_at,
            last_event_id=int(row.last_event_id),
        )

    async def interleaved(db, pair_id, interval, start_, end_):
        rows, st = await original(db, pair_id, interval, start_, end_)
        if not fired["done"] and st is not None and int(st.last_event_id) == 2:
            fired["done"] = True
            snap_rows = [_snapshot(row) for row in rows]
            snap_state = SimpleNamespace(
                pair_id=int(st.pair_id), last_event_id=int(st.last_event_id), last_trade_id=0,
                history_version=str(st.history_version),
                history_ready=bool(st.history_ready),
            )
            # Commit the tail trade exactly as the flusher would, on the same
            # connection, and expire the request's ORM objects.
            candle = await db.get(FxCandle, (pid, interval, start_))
            candle.gold_volume = candle.gold_volume + Decimal("3")
            candle.n_trades = candle.n_trades + 1
            state_row = await db.get(FxMarketDataState, pid)
            state_row.last_event_id = 3
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
    assert resp.headers["x-fx-through-event-id"] == "3"
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
async def test_chart_offboundary_rollup_reports_aligned_bucket_start(client):
    """A 5m rollup of a 12:03 fine bar must report bucket_start 12:00, not 12:03."""
    pid = await _seed_pair()
    await _seed_state(pid, ready=True, cursor=0)
    base = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    await _seed_candle(pid, "1m", base + timedelta(minutes=3), v="2", n=1)
    await _seed_trade(pid, 1, base + timedelta(minutes=4), volume="3")

    resp = await client.get(
        f"/api/v1/fx/pairs/{pid}/chart",
        params={"interval": "5m", "from": base.isoformat(),
                "to": (base + timedelta(minutes=5)).isoformat()},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    reported = datetime.fromisoformat(body[0]["bucket_start"].replace("Z", "+00:00"))
    assert reported == base
    assert Decimal(str(body[0]["volume"])) == Decimal("5")
    assert resp.headers["x-fx-through-event-id"] == "1"


@pytest.mark.asyncio
async def test_chart_coverage_never_claims_a_trade_outside_the_served_window(client):
    """A trade beyond the aligned end must not be claimed (its live delta is kept)."""
    pid = await _seed_pair()
    await _seed_state(pid, ready=True, cursor=0)
    base = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    await _seed_trade(pid, 5, base + timedelta(minutes=1, seconds=30), volume="9")

    resp = await client.get(
        f"/api/v1/fx/pairs/{pid}/chart",
        params={"interval": "1m", "from": base.isoformat(),
                "to": (base + timedelta(minutes=1)).isoformat()},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-fx-through-event-id"] == "0"
    assert resp.json() == []


@pytest.mark.asyncio
async def test_chart_aligned_window_covers_a_midbucket_end_trade(client):
    """With a mid-bucket `to`, the full aligned bucket is served and covered."""
    pid = await _seed_pair()
    await _seed_state(pid, ready=True, cursor=0)
    base = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    await _seed_trade(pid, 6, base + timedelta(seconds=40), volume="4")

    resp = await client.get(
        f"/api/v1/fx/pairs/{pid}/chart",
        params={"interval": "1m", "from": base.isoformat(),
                "to": (base + timedelta(seconds=30)).isoformat()},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-fx-through-event-id"] == "6"
    body = resp.json()
    assert len(body) == 1
    assert Decimal(str(body[0]["volume"])) == Decimal("4")


@pytest.mark.asyncio
async def test_fx_volume_24h_single_snapshot_survives_midread_flush():
    """One-statement read: a flush after execution cannot create a zero count.

    The wrapper flushes the outstanding trade after the single statement has
    executed but before its values are read.  A multi-observation implementation
    would see the post-flush cursor and then find no tail above it (total 0);
    the single statement keeps the pre-flush composition (7).
    """
    pid = await _seed_pair()
    await _seed_state(pid, ready=True, cursor=2)
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    trade_at = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
    await _seed_trade(pid, 3, trade_at, volume="7")

    class _One:
        def __init__(self, row):
            self._row = row

        def one(self):
            return self._row

    class Interleaving:
        def __init__(self, real):
            self._real = real
            self.calls = 0

        async def execute(self, stmt, *args, **kwargs):
            result = await self._real.execute(stmt, *args, **kwargs)
            self.calls += 1
            if self.calls == 1:
                row = result.one()   # materialise the single statement snapshot
                # The flusher commits the outstanding trade on the same
                # connection after the statement ran.
                self._real.add(FxCandle(
                    pair_id=pid, interval="1m", bucket_start=trade_at,
                    open_price=Decimal("1"), high_price=Decimal("1"),
                    low_price=Decimal("1"), close_price=Decimal("1"),
                    gold_volume=Decimal("7"), n_trades=1,
                    first_event_at=trade_at, first_event_id=3,
                    last_event_at=trade_at, last_event_id=3,
                ))
                state_row = await self._real.get(FxMarketDataState, pid)
                state_row.last_event_id = 3
                await self._real.commit()
                return _One(row)
            return result

        async def commit(self):
            await self._real.commit()

    async with async_session_maker() as real:
        proxy = Interleaving(real)
        total = await fx_volume_24h(proxy, pid, now)

    assert total == Decimal("7")
    assert proxy.calls == 1
    async with async_session_maker() as db:
        cursor = (await db.get(FxMarketDataState, pid)).last_event_id
    assert int(cursor) == 3   # the mid-read flush really happened


@pytest.mark.asyncio
async def test_history_lock_unavailable_is_a_non_cache_503():
    """A connection that cannot take the pair lock never emits an immutable 200."""
    from sqlalchemy.exc import OperationalError

    from app.services.fx import market_reads

    pid = await _seed_pair()
    version = await _seed_state(pid, ready=True, cursor=0)
    seg = _previous_segment(3600)
    await _seed_candle(pid, "1m", datetime.fromtimestamp(seg + 60, tz=UTC))

    class LockFailSession:
        def __init__(self, real):
            self._real = real

        async def execute(self, stmt, *args, **kwargs):
            if getattr(stmt, "_for_update_arg", None) is not None:
                raise OperationalError(
                    "SELECT ... FOR SHARE", {}, Exception("cannot execute in a read-only transaction"))
            return await self._real.execute(stmt, *args, **kwargs)

        async def commit(self):
            await self._real.commit()

    async with async_session_maker() as real:
        with pytest.raises(HTTPException) as exc:
            await market_reads.read_fx_history(
                LockFailSession(real), pid, version, "1m", seg)
    assert exc.value.status_code == 503


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


async def _seed_fund_event(pid: int, at: datetime, *, pre="1", post="1.5", initialize=False) -> int:
    async with async_session_maker() as db:
        audit = AuditEvent(event_type="fx_fund", ref_table="fx_pair", ref_id=pid, ts=at)
        db.add(audit)
        await db.flush()
        event = append_pool_event(db, pair_id=pid, kind="initialize" if initialize else "fund",
                                  created_at=at, pre_reserves=None if initialize else (Decimal(pre), Decimal("1")),
                                  post_reserves=(Decimal(post), Decimal("1")), gold_volume=Decimal("0"),
                                  audit_id=audit.id)
        await db.flush()
        result = event.id
        await db.commit()
        return result


@pytest.mark.asyncio
async def test_chart_uses_prior_event_anchor_and_keeps_real_zero_volume_ohlc():
    pid = await _seed_pair()
    start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    anchor = await _seed_fund_event(pid, start - timedelta(minutes=1), post="1", initialize=True)
    fund = await _seed_fund_event(pid, start + timedelta(minutes=2, seconds=5), post="1.5")
    await _seed_state(pid, ready=True, cursor=anchor)
    async with async_session_maker() as db:
        rows = await read_fx_chart(db, pid, "1m", start, start + timedelta(minutes=4))
        volume = await fx_volume_24h(db, pid, start + timedelta(minutes=4))
    assert len(rows) == 4
    assert [row["n_events"] for row in rows] == [0, 0, 1, 0]
    assert [row["close"] for row in rows] == [Decimal("1"), Decimal("1"), Decimal("1.5"), Decimal("1.5")]
    assert rows[2]["open"] == Decimal("1") and rows[2]["high"] == Decimal("1.5")
    assert all(row["volume"] == 0 and row["n_trades"] == 0 for row in rows)
    assert volume == 0 and fund > anchor


@pytest.mark.asyncio
async def test_chart_without_historical_anchor_never_fills_current_pair_price():
    pid = await _seed_pair()
    await _seed_state(pid, ready=True, cursor=0)
    start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    async with async_session_maker() as db:
        assert await read_fx_chart(db, pid, "1m", start, start + timedelta(minutes=3)) == []


@pytest.mark.asyncio
async def test_excess_event_tail_returns_retryable_503_in_chart_and_stream_snapshot(client, monkeypatch):
    from app.services.fx import market_reads
    monkeypatch.setattr(market_reads, "MAX_TAIL_EVENTS", 2)
    pid = await _seed_pair()
    now = datetime.now(UTC).replace(second=30, microsecond=0)
    anchor = await _seed_fund_event(pid, now - timedelta(minutes=1), post="1", initialize=True)
    await _seed_state(pid, ready=True, cursor=anchor)
    for seconds in (1, 2, 3):
        await _seed_fund_event(pid, now.replace(second=seconds), pre="1", post="1")
    response = await client.get(f"/api/v1/fx/pairs/{pid}/chart", params={
        "interval": "1m", "from": now.replace(second=0).isoformat(), "to": now.isoformat()})
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    async with async_session_maker() as db:
        with pytest.raises(HTTPException) as exc:
            await market_reads.read_fx_stream_history(db, pid, now)
        assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_unflushed_funding_before_window_supplies_carried_close_and_event_coverage(client):
    pid = await _seed_pair()
    start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    initialize = await _seed_fund_event(pid, start - timedelta(hours=1), post="1", initialize=True)
    await _seed_state(pid, ready=True, cursor=initialize)
    fund = await _seed_fund_event(pid, start - timedelta(seconds=1), post="1.5")
    response = await client.get(f"/api/v1/fx/pairs/{pid}/chart", params={
        "interval": "1m", "from": start.isoformat(), "to": (start + timedelta(minutes=2)).isoformat()})
    assert response.status_code == 200
    assert response.headers["x-fx-through-event-id"] == str(fund)
    assert len(response.json()) == 2
    assert all(row["close"] == 1.5 and row["n_events"] == 0 for row in response.json())


@pytest.mark.asyncio
async def test_chart_flush_before_joined_read_refreshes_cached_checkpoint(client, monkeypatch):
    """A separate-session flush must not pair new candles with the cached old cursor."""
    from sqlalchemy import select
    from app.services.fx import market_reads
    from app.services.fx.candles import apply_candle_batch, compute_fx_candle_rows
    pid = await _seed_pair()
    version = await _seed_state(pid, ready=True, cursor=2)
    start = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    await _seed_candle(pid, "1m", start, v="2", n=2)
    await _seed_trade(pid, 3, start + timedelta(seconds=20), volume="3")
    original = market_reads._load_candles_with_state
    fired = False

    async def flush_then_join(db, pair_id, interval, start_, end_):
        nonlocal fired
        if not fired:
            fired = True
            # Keep an old checkpoint in the identity map while another
            # transaction flushes. The joined read must refresh this instance.
            cached_checkpoint = await db.get(FxMarketDataState, pair_id)
            async with async_session_maker() as writer:
                async with writer.begin():
                    event = (await writer.execute(select(FxPoolEvent).where(FxPoolEvent.id == 3))).scalar_one()
                    await apply_candle_batch(writer, pair_id=pid, expected_event_id=2,
                                             through_event_id=3, rows=compute_fx_candle_rows([event]),
                                             history_version=version)
        return await original(db, pair_id, interval, start_, end_)

    monkeypatch.setattr(market_reads, "_load_candles_with_state", flush_then_join)
    response = await client.get(f"/api/v1/fx/pairs/{pid}/chart", params={
        "interval": "1m", "from": start.isoformat(), "to": (start + timedelta(minutes=1)).isoformat()})
    assert response.status_code == 200, response.text
    assert response.headers["x-fx-through-event-id"] == "3"
    point, = response.json()
    assert Decimal(str(point["volume"])) == Decimal("5")
    assert point["n_trades"] == point["n_events"] == 3


@pytest.mark.asyncio
async def test_stream_lock_wait_crossing_bucket_boundary_never_claims_absent_event(monkeypatch):
    """Committed boundary funding is represented at the post-lock snapshot time."""
    import asyncio
    import json
    from types import SimpleNamespace
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from app.core.database import engine
    from app.api.v1 import fx_stream
    from app.services.realtime import MarketEventBroker

    pid = await _seed_pair()
    boundary = datetime(2026, 10, 1, 12, 1, tzinfo=UTC)
    before = boundary - timedelta(seconds=1)
    after = boundary + timedelta(seconds=2)
    initialize = await _seed_fund_event(pid, boundary - timedelta(minutes=1), post="1", initialize=True)
    await _seed_state(pid, ready=True, cursor=initialize)
    entered, release = asyncio.Event(), asyncio.Event()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return after if release.is_set() else before

    class WaitingSession(AsyncSession):
        waited = False

        async def execute(self, statement, *args, **kwargs):
            if not self.waited:
                self.waited = True
                entered.set()
                await release.wait()
            return await super().execute(statement, *args, **kwargs)

    waiting_factory = async_sessionmaker(engine, class_=WaitingSession, expire_on_commit=False)
    calls = 0

    def reader_factory():
        nonlocal calls
        calls += 1
        return waiting_factory() if calls == 2 else async_session_maker()

    monkeypatch.setattr(fx_stream, "datetime", Clock)
    monkeypatch.setattr(fx_stream, "async_session_maker", reader_factory)
    monkeypatch.setattr(fx_stream, "BROKER", MarketEventBroker())
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="boundary-test"))
    response = await fx_stream.stream(pid, request)
    packet = asyncio.create_task(response.body_iterator.__anext__())
    await entered.wait()
    fund = await _seed_fund_event(pid, boundary, pre="1", post="1.5")
    release.set()
    try:
        wire = json.loads((await packet).decode().split("data: ", 1)[1])["data"]
        assert wire["history_tail_through_event_id"] == fund
        minute = wire["history_tail"]["1m"]
        at = (int(boundary.timestamp()) - minute["t0"]) // minute["step"]
        index = minute["t"].index(at)
        assert Decimal(minute["c"][index]) == Decimal("1.5")
        assert minute["events"][index] == 1 and minute["trades"][index] == 0
        assert wire["history_tail_at"] == after.isoformat()
    finally:
        await response.body_iterator.aclose()


async def _warm_chart_ring(pid: int, start: datetime) -> int:
    from sqlalchemy import select
    from app.services.fx.candles import apply_candle_batch, compute_fx_candle_rows
    await _seed_fund_event(pid, start - timedelta(minutes=1), post="1", initialize=True)
    last = await _seed_fund_event(pid, start + timedelta(seconds=5), post="1.5")
    await _seed_state(pid, ready=True, cursor=0)
    async with async_session_maker() as writer:
        events = (await writer.execute(select(FxPoolEvent).where(FxPoolEvent.pair_id == pid))).scalars().all()
        await apply_candle_batch(writer, pair_id=pid, expected_event_id=0,
                                 through_event_id=last, rows=compute_fx_candle_rows(events),
                                 history_version="gen-1")
        await writer.commit()
    await FX_MARKET_DATA.start(write_owner=False)
    assert FX_MARKET_DATA.get_candles(pid, "1m", start, start + timedelta(minutes=1)) is not None
    return last


@pytest.mark.asyncio
@pytest.mark.parametrize("before_window", [False, True])
async def test_warm_chart_committed_event_after_ring_copy_falls_back(monkeypatch, before_window):
    """A post-copy event in the window or its anchor cannot be claimed by stale ring rows."""
    from app.services.fx import market_reads
    pid = await _seed_pair()
    start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=2)
    await _warm_chart_ring(pid, start)
    original = market_reads._chart_window_bounds
    committed = None

    async def commit_then_bounds(db, pair_id, start_, end_):
        nonlocal committed
        at = start_ - timedelta(seconds=1) if before_window else start_ + timedelta(seconds=20)
        committed = await _seed_fund_event(pid, at, pre="1.5", post="2")
        return await original(db, pair_id, start_, end_)

    monkeypatch.setattr(market_reads, "_chart_window_bounds", commit_then_bounds)
    try:
        async with async_session_maker() as db:
            body, coverage, version = await market_reads.read_fx_chart_with_meta(
                db, pid, "1m", start, start + timedelta(minutes=1))
        assert coverage == committed and version == "gen-1"
        point, = body
        assert point["volume"] == 0 and point["n_trades"] == 0
        assert point["n_events"] == (1 if before_window else 2)
        assert point["close"] == Decimal("1.5" if before_window else "2")
        if not before_window:
            assert point["high"] == Decimal("2")
    finally:
        await FX_MARKET_DATA.stop()


@pytest.mark.asyncio
async def test_warm_chart_rebuild_after_bounds_rejects_old_generation(monkeypatch):
    from app.services.fx import market_reads
    pid = await _seed_pair()
    start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=2)
    await _warm_chart_ring(pid, start)
    original = market_reads._chart_window_bounds

    async def rotate_after_bounds(db, pair_id, start_, end_):
        result = await original(db, pair_id, start_, end_)
        async with async_session_maker() as writer:
            state = await writer.get(FxMarketDataState, pid)
            state.history_version = "gen-2"
            await writer.commit()
        return result

    monkeypatch.setattr(market_reads, "_chart_window_bounds", rotate_after_bounds)
    try:
        async with async_session_maker() as db:
            with pytest.raises(HTTPException) as exc:
                await read_fx_chart(db, pid, "1m", start, start + timedelta(minutes=1))
        assert exc.value.status_code == 503
        assert exc.value.headers["Retry-After"] == "5"
    finally:
        await FX_MARKET_DATA.stop()
