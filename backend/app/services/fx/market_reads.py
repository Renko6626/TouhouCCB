"""FX materialised read paths: chart, immutable history segments, rolling 24h.

This module is the read half of the FX market-data route (plan Task 4).  It
never owns the runtime or storage: it reads the incremental ring through the
frozen ``FX_MARKET_DATA`` API, falls back to the persisted ``fx_candle`` rows
plus only the *projected* unflushed trade tail, and never rebuilds a full day of
``FxTrade`` ORM objects.

Hard rules honoured here:

* ``/chart`` validates interval and bucket count **before** touching the
  database, preserves the legacy response shape
  (``bucket_start, interval, open, high, low, close, volume``) and refuses an
  un-backfilled pair with an explicit retryable 503 instead of scanning raw
  trades.
* ``/history/fx/...`` follows the LMSR three-line defence (sealed segment, ring
  first, persisted high-water second) but uses the FX generation/readiness and
  the FX flusher, never the LMSR one.  A closed segment is only returned as an
  immutable 200 after an indexed ``EXISTS`` proves no committed trade after the
  durable cursor still falls inside it.
* Rolling 24h gold volume is a minute-candle aggregate plus raw SQL sums at the
  two partial window edges and the outstanding unflushed tail.  When the pair is
  not ready it returns ``None`` so the caller keeps the exact SQL ``SUM``
  fallback.

The requested chart interval keeps the four native periods ``10s/1m/15m/1h``.
Legacy ``Nm/Nh/Nd`` intervals are still accepted by rolling the persisted
candles up from the largest native base that divides them; intervals that no
native period divides are rejected (see the handoff report for the exact scope).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

from fastapi import HTTPException
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fx import FxCandle, FxMarketDataState, FxPair, FxTrade
from app.services.fx.candles import compute_fx_candle_rows, merge_row
from app.services.fx.market_state import FX_MARKET_DATA
from app.services.history_ring import RING_SPEC

UTC = timezone.utc

#: Hard ceiling on a chart response (spec: at most 20,000 buckets).
MAX_CHART_BUCKETS = 20000

#: Native storage periods, largest first, for legacy rollup base selection.
_NATIVE_STEPS: tuple[tuple[str, int], ...] = tuple(
    sorted(((name, tier.step) for name, tier in RING_SPEC.items()),
           key=lambda item: item[1], reverse=True)
)

_LEGACY_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}

#: Process LRU for immutable FX segments.  The history generation is part of the
#: key, so a rebuilt pair can never be served a segment from the old history.
_FX_LRU_MAX = 1024
_fx_lru: "OrderedDict[tuple[str, int, str, str, int], dict]" = None  # type: ignore[assignment]


def _lru() -> "OrderedDict[tuple[str, int, str, str, int], dict]":
    global _fx_lru
    if _fx_lru is None:
        from collections import OrderedDict
        _fx_lru = OrderedDict()
    return _fx_lru


def clear_fx_history_cache() -> None:
    """Drop the process segment cache (tests / explicit invalidation)."""
    _lru().clear()


def _lru_get(key):
    enc = _lru().get(key)
    if enc is not None:
        _lru().move_to_end(key)
    return enc


def _lru_put(key, enc) -> None:
    cache = _lru()
    cache[key] = enc
    cache.move_to_end(key)
    while len(cache) > _FX_LRU_MAX:
        cache.popitem(last=False)


# ── time / value helpers ────────────────────────────────────────────────────
def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _bucket_start(ts: datetime, seconds: int) -> datetime:
    epoch = int(_utc(ts).timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=UTC)


def _ceil_bucket(ts: datetime, seconds: int) -> datetime:
    """Smallest multiple-of-``seconds`` instant at or after ``ts``."""
    epoch = math.ceil(_utc(ts).timestamp())
    return datetime.fromtimestamp(epoch + ((-epoch) % seconds), tz=UTC)


# ── interval resolution ─────────────────────────────────────────────────────
class _IntervalPlan:
    __slots__ = ("interval", "seconds", "native", "base", "base_step")

    def __init__(self, interval: str, seconds: int, native: str | None, base: str | None) -> None:
        self.interval = interval
        self.seconds = int(seconds)
        self.native = native
        self.base = base
        self.base_step = RING_SPEC[base].step if base else self.seconds


def _legacy_seconds(raw: str) -> int:
    units = _LEGACY_UNIT_SECONDS
    if len(raw) < 2 or raw[-1] not in units:
        raise ValueError("interval must be 10s, Nm, Nh, or Nd")
    try:
        amount = int(raw[:-1])
    except ValueError as exc:
        raise ValueError("interval must be 10s, Nm, Nh, or Nd") from exc
    if amount <= 0 or amount > 1440:
        raise ValueError("interval is out of range")
    return amount * units[raw[-1]]


def resolve_chart_interval(interval: str) -> _IntervalPlan:
    """Validate an interval and pick how it is read.

    Native ``10s/1m/15m/1h`` are read straight from their own column.  Legacy
    ``Nm/Nh/Nd`` are rolled up from the largest native period that divides them.
    """
    raw = str(interval).strip().lower()
    if raw in RING_SPEC:
        return _IntervalPlan(raw, RING_SPEC[raw].step, native=raw, base=raw)
    seconds = _legacy_seconds(raw)
    for name, step in _NATIVE_STEPS:
        if seconds % step == 0:
            return _IntervalPlan(raw, seconds, native=None, base=name)
    raise ValueError("interval is not a supported multiple of 10s/1m/15m/1h")


def chart_bucket_count(plan: _IntervalPlan, start: datetime, end: datetime) -> int:
    return int(math.ceil((_utc(end) - _utc(start)).total_seconds() / plan.seconds))


# ── persisted candle adaptation ─────────────────────────────────────────────
def _row_to_agg(row: Any) -> dict:
    return {
        "pair_id": int(row.pair_id),
        "interval": row.interval,
        "bucket_start": _utc(row.bucket_start),
        "open_price": _dec(row.open_price),
        "high_price": _dec(row.high_price),
        "low_price": _dec(row.low_price),
        "close_price": _dec(row.close_price),
        "gold_volume": _dec(row.gold_volume),
        "n_trades": int(row.n_trades),
        "first_trade_at": _utc(row.first_trade_at),
        "first_trade_id": int(row.first_trade_id),
        "last_trade_at": _utc(row.last_trade_at),
        "last_trade_id": int(row.last_trade_id),
    }


def _chart_row(interval: str, agg: dict) -> dict:
    """Old ``/chart`` field names (``open``/``close``/``volume``), Decimal kept."""
    return {
        "bucket_start": agg["bucket_start"],
        "interval": interval,
        "open": _dec(agg["open_price"]),
        "high": _dec(agg["high_price"]),
        "low": _dec(agg["low_price"]),
        "close": _dec(agg["close_price"]),
        "volume": _dec(agg["gold_volume"]),
    }


async def _read_state(db: AsyncSession, pair_id: int) -> FxMarketDataState | None:
    """Read the durable state row without creating it (read paths never write)."""
    return (await db.execute(
        select(FxMarketDataState).where(FxMarketDataState.pair_id == int(pair_id))
    )).scalars().first()


async def _load_candles(db: AsyncSession, pair_id: int, interval: str,
                        start: datetime, end: datetime) -> list[Any]:
    if start >= end:
        return []
    return list((await db.execute(
        select(FxCandle).where(
            FxCandle.pair_id == int(pair_id),
            FxCandle.interval == interval,
            FxCandle.bucket_start >= start,
            FxCandle.bucket_start < end,
        ).order_by(FxCandle.bucket_start.asc())
    )).scalars().all())


async def _load_tail_trades(db: AsyncSession, pair_id: int, after_id: int,
                            start: datetime, end: datetime, *,
                            max_id: int | None = None) -> list[Any]:
    """Projected committed trades after ``after_id`` inside ``[start, end)``.

    Only the aggregation columns are selected; a full ``FxTrade`` ORM row set is
    never materialised.  The durable cursor normally trails by one flush tick.
    ``max_id`` caps the tail at a captured highwater so the chart's coverage
    header can describe exactly the rows returned.
    """
    if start >= end:
        return []
    stmt = select(
        FxTrade.id, FxTrade.pair_id, FxTrade.created_at, FxTrade.post_price,
        FxTrade.side, FxTrade.input_amount, FxTrade.output_amount,
    ).where(
        FxTrade.pair_id == int(pair_id),
        FxTrade.id > int(after_id),
        FxTrade.created_at >= start,
        FxTrade.created_at < end,
    )
    if max_id is not None:
        stmt = stmt.where(FxTrade.id <= int(max_id))
    return list((await db.execute(
        stmt.order_by(FxTrade.created_at.asc(), FxTrade.id.asc())
    )).all())


async def _load_candles_with_state(db: AsyncSession, pair_id: int, interval: str,
                                   start: datetime, end: datetime) -> tuple[list[Any], Any]:
    """Candle rows in the window **and** the persisted state in one statement.

    The ``LEFT OUTER JOIN`` starts from the state so the row is returned even
    when the window has no candles, and the candle rows and the durable
    checkpoint therefore share the statement's MVCC snapshot.  A flush that
    commits afterwards cannot leave candles inconsistent with the cursor the
    caller reads here.
    """
    if start >= end:
        start = end
    condition = and_(
        FxCandle.pair_id == FxMarketDataState.pair_id,
        FxCandle.interval == interval,
        FxCandle.bucket_start >= start,
        FxCandle.bucket_start < end,
    )
    stmt = (
        select(FxMarketDataState, FxCandle)
        .select_from(FxMarketDataState)
        .outerjoin(FxCandle, condition)
        .where(FxMarketDataState.pair_id == int(pair_id))
        .order_by(FxCandle.bucket_start.asc())
    )
    state = None
    rows: list[Any] = []
    for state_row, candle_row in (await db.execute(stmt)).all():
        state = state_row
        if candle_row is not None:
            rows.append(candle_row)
    return rows, state


async def _pair_max_trade_id(db: AsyncSession, pair_id: int) -> int:
    """Indexed observed highwater of committed pair trades (never a global max)."""
    value = (await db.execute(
        select(func.max(FxTrade.id)).where(FxTrade.pair_id == int(pair_id))
    )).scalar()
    return int(value or 0)


async def _capture_highwater(db: AsyncSession, pair_id: int, version: str) -> int:
    """Highwater cap for the DB-fallback raw tail.

    Prefers the runtime's in-memory applied cursor when it knows the **same**
    generation; otherwise observes the committed pair max.  Either way it is
    captured after the candle+state statement, so the tail can only extend the
    body beyond the durable cursor, never re-describe candles we already have.
    """
    rt = FX_MARKET_DATA.state(int(pair_id))
    if (rt is not None and rt.get("history_ready")
            and str(rt.get("history_version")) == str(version)):
        return int(rt.get("applied_trade_id") or 0)
    return await _pair_max_trade_id(db, pair_id)


async def _has_trade_in_segment_after(db: AsyncSession, pair_id: int, after_id: int,
                                      start: datetime, end: datetime) -> bool:
    """Indexed EXISTS: any committed trade with ``id > after_id`` in the segment.

    This is the immutable-200 proof for a closed segment: a trade that is
    committed but not covered by ``after_id`` (durable cursor, or the ring's
    applied cursor) could still change the segment, so it must not be cached.
    """
    stmt = (
        select(FxTrade.id)
        .where(
            FxTrade.pair_id == int(pair_id),
            FxTrade.id > int(after_id),
            FxTrade.created_at >= start,
            FxTrade.created_at < end,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first() is not None


def _require_pair(db_pair: FxPair | None, pair_id: int) -> FxPair:
    if db_pair is None:
        raise HTTPException(status_code=404, detail="FX pair not found")
    return db_pair


# ── /chart ──────────────────────────────────────────────────────────────────
async def read_fx_chart_with_meta(db: AsyncSession, pair_id: int, interval: str,
                                  start: datetime, end: datetime) -> tuple[list[dict], int, str]:
    """Old ``/chart`` body **plus** the exact coverage cursor and generation.

    Returns ``(body, through_trade_id, history_version)``.  ``through_trade_id``
    is the pair highwater whose data the returned rows fully describe: every
    committed trade with ``id <= through`` inside the window is in ``body`` and
    no trade above it is.  The route exposes the pair as the
    ``X-FX-Through-Trade-ID`` / ``X-FX-History-Version`` response headers; the
    body itself stays the legacy JSON list.

    Validation (range, interval, 20,000-bucket ceiling) happens before any query.
    Missing metadata is an explicit 503, never a fabricated coverage value.
    """
    start, end = _utc(start), _utc(end)
    if start >= end:
        raise HTTPException(status_code=422, detail="from must be earlier than to")
    try:
        plan = resolve_chart_interval(interval)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if chart_bucket_count(plan, start, end) > MAX_CHART_BUCKETS:
        raise HTTPException(
            status_code=422,
            detail=f"chart window exceeds {MAX_CHART_BUCKETS} buckets",
        )

    pair_id = int(pair_id)
    _require_pair(await db.get(FxPair, pair_id), pair_id)
    state = await _read_state(db, pair_id)
    if state is None or not state.history_ready:
        # No complete history yet: retryable, never a full raw-trade rebuild.
        raise HTTPException(status_code=503, detail="FX history is not ready")
    version = str(state.history_version)

    if plan.native is not None:
        # ``get_candles`` and ``state`` are synchronous and back-to-back with no
        # await between them, so the copied ring rows and the applied cursor are
        # captured atomically under the same event-loop step.
        ring_rows = FX_MARKET_DATA.get_candles(pair_id, plan.native, start, end)
        rt = FX_MARKET_DATA.state(pair_id)
        if (ring_rows is not None and rt is not None and rt.get("history_ready")
                and str(rt.get("history_version")) == version):
            return ([_chart_row(plan.interval, row) for row in ring_rows],
                    int(rt.get("applied_trade_id") or 0), version)

    # Persisted base candles + the persisted state in ONE statement, then only
    # the projected unflushed tail above the durable cursor capped at a captured
    # highwater.  A flush that commits mid-request can only appear in the tail.
    first_bucket = _bucket_start(start, plan.seconds)
    last_bucket = _bucket_start(end - timedelta(microseconds=1), plan.seconds)
    query_end = last_bucket + timedelta(seconds=plan.seconds)
    base_interval = plan.base or plan.native or plan.interval
    base_rows, db_state = await _load_candles_with_state(
        db, pair_id, base_interval, first_bucket, query_end,
    )
    if db_state is None or not db_state.history_ready:
        raise HTTPException(status_code=503, detail="FX history is not ready")
    # The generation travels with the candle rows from the same statement, so a
    # rebuild in flight can never mix two generations in one body.
    db_version = str(db_state.history_version)
    durable = int(db_state.last_trade_id)

    highwater = await _capture_highwater(db, pair_id, db_version)
    through = max(durable, highwater)

    merged: dict[datetime, dict] = {}
    for row in base_rows:
        agg = _row_to_agg(row)
        bucket = _bucket_start(agg["bucket_start"], plan.seconds)
        existing = merged.get(bucket)
        merged[bucket] = agg if existing is None else merge_row(existing, agg)

    tail_trades = await _load_tail_trades(db, pair_id, durable, start, end, max_id=through)
    for row in compute_fx_candle_rows(tail_trades):
        if row["interval"] != base_interval:
            continue
        bucket = _bucket_start(row["bucket_start"], plan.seconds)
        existing = merged.get(bucket)
        merged[bucket] = row if existing is None else merge_row(existing, row)

    return ([_chart_row(plan.interval, merged[bucket]) for bucket in sorted(merged)],
            through, db_version)


async def read_fx_chart(db: AsyncSession, pair_id: int, interval: str,
                        start: datetime, end: datetime) -> list[dict]:
    """Backward-compatible body-only wrapper around :func:`read_fx_chart_with_meta`."""
    body, _through, _version = await read_fx_chart_with_meta(db, pair_id, interval, start, end)
    return body


# ── /history/fx/... ─────────────────────────────────────────────────────────
def _encode_segment(rows: Iterable[dict], interval: str, t0: int, until: int) -> dict:
    """Columnar segment with Decimal strings (never LMSR's float x 1e8 codec)."""
    tier = RING_SPEC[interval]
    step = tier.step
    out: dict[str, Any] = {
        "t0": t0,
        "step": step,
        "n_buckets": (until - t0) // step,
        "t": [],
        "o": [],
        "h": [],
        "l": [],
        "c": [],
        "v": [],
        "trades": [],
    }
    ordered = sorted(rows, key=lambda row: _utc(row["bucket_start"]))
    for row in ordered:
        epoch = int(_utc(row["bucket_start"]).timestamp())
        if not (t0 <= epoch < until):
            continue
        out["t"].append((epoch - t0) // step)
        out["o"].append(str(_dec(row["open_price"])))
        out["h"].append(str(_dec(row["high_price"])))
        out["l"].append(str(_dec(row["low_price"])))
        out["c"].append(str(_dec(row["close_price"])))
        out["v"].append(str(_dec(row["gold_volume"])))
        out["trades"].append(int(row["n_trades"]))
    return out


async def read_fx_history(db: AsyncSession, pair_id: int, history_version: str,
                          interval: str, segment_epoch: int) -> dict:
    """One immutable FX segment; raises HTTPException on any incomplete state."""
    tier = RING_SPEC.get(interval)
    if tier is None:
        raise HTTPException(status_code=404, detail="不支持的 interval")
    seg_len = tier.segment
    if segment_epoch < 0 or segment_epoch % seg_len:
        raise HTTPException(status_code=404, detail="segment_epoch 未对齐段长")

    now_epoch = int(datetime.now(UTC).timestamp())
    seg_end = segment_epoch + seg_len
    if seg_end > now_epoch:
        raise HTTPException(status_code=404, detail="段尚未封存")

    pair_id = int(pair_id)
    state = await _read_state(db, pair_id)
    if state is None or not state.history_ready:
        raise HTTPException(status_code=503, detail="FX 历史未就绪，请稍后重试")
    if str(state.history_version) != str(history_version):
        # The URL names a generation that is no longer current: the segment can
        # never be served again, and must not be mixed into the new history.
        raise HTTPException(status_code=404, detail="历史版本已失效")

    # Cache key is product + pair + history generation + period + segment.
    key = ("fx", pair_id, str(history_version), interval, int(segment_epoch))
    cached = _lru_get(key)
    if cached is not None:
        return cached

    seg_start_dt = datetime.fromtimestamp(segment_epoch, tz=UTC)
    seg_end_dt = datetime.fromtimestamp(seg_end, tz=UTC)

    # Defence 2: the owner's ring is written in real time; when it fully covers
    # the closed segment and no committed trade is past its applied cursor the
    # segment is complete without waiting for the flusher.
    rt = FX_MARKET_DATA.state(pair_id)
    if (rt is not None and rt.get("history_ready")
            and str(rt.get("history_version")) == str(history_version)):
        ring_rows = FX_MARKET_DATA.get_candles(pair_id, interval, seg_start_dt, seg_end_dt)
        if ring_rows is not None:
            applied = int(rt.get("applied_trade_id") or 0)
            if not await _has_trade_in_segment_after(db, pair_id, applied,
                                                     seg_start_dt, seg_end_dt):
                enc = _encode_segment(ring_rows, interval, segment_epoch, seg_end)
                _lru_put(key, enc)
                return enc

    # Defence 3: persisted candles.  Refuse while a queued FX flush or a
    # committed trade past the durable cursor could still change the segment.
    pending = FX_MARKET_DATA.flusher.oldest_pending_bucket(pair_id)
    if pending is not None and _utc(pending) < seg_end_dt:
        raise HTTPException(status_code=503, detail="段落库未完成，请稍后重试")
    durable = int(state.last_trade_id)
    if await _has_trade_in_segment_after(db, pair_id, durable, seg_start_dt, seg_end_dt):
        raise HTTPException(status_code=503, detail="段落库未完成，请稍后重试")

    rows = await _load_candles(db, pair_id, interval, seg_start_dt, seg_end_dt)
    enc = _encode_segment((_row_to_agg(row) for row in rows), interval,
                          segment_epoch, seg_end)
    _lru_put(key, enc)
    return enc


# ── public snapshot metadata (homepage bootstrap, no per-card SSE) ──────────
async def read_fx_snapshot_metadata(db: AsyncSession, pair_id: int,
                                    now: datetime) -> dict[str, Any]:
    """Lightweight public history metadata for the HTTP ``FxSnapshot``.

    Returns only the two additive public fields the cached-history loader needs
    to bootstrap without a per-card SSE connection: ``history_version`` (opaque
    UUID or ``None``) and ``history_ready`` (``False`` when the pair has no
    persisted state yet).  No four-period tail is built here.  ``now`` is part
    of the frozen integration signature (reserved for a future freshness field)
    and is otherwise unused.
    """
    state = await _read_state(db, pair_id)
    if state is None:
        return {"history_version": None, "history_ready": False}
    return {
        "history_version": str(state.history_version),
        "history_ready": bool(state.history_ready),
    }


# ── rolling 24h gold volume ─────────────────────────────────────────────────
def _gold_sum_expr():
    return func.coalesce(
        func.sum(case((FxTrade.side == "buy", FxTrade.input_amount),
                      else_=FxTrade.output_amount)),
        Decimal("0"),
    )


async def _raw_gold_sum(db: AsyncSession, pair_id: int, start: datetime, end: datetime,
                        *, after_id: int | None = None) -> Decimal:
    if start >= end:
        return Decimal("0")
    stmt = select(_gold_sum_expr()).where(
        FxTrade.pair_id == int(pair_id),
        FxTrade.created_at >= start,
        FxTrade.created_at < end,
    )
    if after_id is not None:
        stmt = stmt.where(FxTrade.id > int(after_id))
    value = (await db.execute(stmt)).scalar_one()
    return Decimal(value) if value is not None else Decimal("0")


async def fx_volume_24h(db: AsyncSession, pair_id: int, now: datetime) -> Decimal | None:
    """Exact rolling-24h gold-side volume, or ``None`` when history is not ready.

    ``None`` lets the caller keep the exact SQL ``SUM`` fallback.  When ready,
    the sum is a minute-candle aggregate for the fully-covered minutes plus raw
    SQL sums for the two partial window edges and for committed trades past the
    durable cursor that are still inside the full-minute range.
    """
    now = _utc(now)
    since = now - timedelta(hours=24)
    state = await _read_state(db, pair_id)
    if state is None or not state.history_ready:
        return None

    b_start = _ceil_bucket(since, 60)
    b_end = _bucket_start(now, 60)
    if b_start > b_end:
        # Window shorter than a minute (or an unaligned tiny window): everything
        # is a partial range.
        b_start = b_end

    total = Decimal("0")
    if b_start < b_end:
        candle_sum = (await db.execute(
            select(func.coalesce(func.sum(FxCandle.gold_volume), Decimal("0"))).where(
                FxCandle.pair_id == int(pair_id),
                FxCandle.interval == "1m",
                FxCandle.bucket_start >= b_start,
                FxCandle.bucket_start < b_end,
            )
        )).scalar_one()
        total += Decimal(candle_sum) if candle_sum is not None else Decimal("0")
        # Read the durable cursor after the candle aggregate so a flush that
        # commits mid-request can only be omitted, never double-counted.
        fresh = await _read_state(db, pair_id)
        durable = int((fresh if fresh is not None else state).last_trade_id)
        total += await _raw_gold_sum(db, pair_id, b_start, b_end, after_id=durable)

    if since < b_start:
        total += await _raw_gold_sum(db, pair_id, since, b_start)
    if b_end < now:
        total += await _raw_gold_sum(db, pair_id, b_end, now)
    return total
