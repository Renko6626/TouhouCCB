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
from sqlalchemy import and_, bindparam, case, func, select, true
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fx import FxCandle, FxMarketDataState, FxPair, FxPoolEvent, FxTrade
from app.services.fx.candles import compute_fx_candle_rows, merge_row
from app.services.fx.market_state import FX_MARKET_DATA
from app.services.history_ring import RING_SPEC

UTC = timezone.utc

#: Hard ceiling on a chart response (spec: at most 20,000 buckets).
MAX_CHART_BUCKETS = 20000
MAX_TAIL_EVENTS = 10000

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
        "n_events": int(row.n_events),
        "first_event_at": _utc(row.first_event_at),
        "first_event_id": int(row.first_event_id),
        "last_event_at": _utc(row.last_event_at),
        "last_event_id": int(row.last_event_id),
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
        "n_trades": int(agg["n_trades"]),
        "n_events": int(agg["n_events"]),
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


async def _load_tail_events(db: AsyncSession, pair_id: int, after_id: int,
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
        FxPoolEvent.id, FxPoolEvent.pair_id, FxPoolEvent.created_at, FxPoolEvent.kind,
        FxPoolEvent.pre_gold_reserve, FxPoolEvent.pre_foreign_reserve,
        FxPoolEvent.post_gold_reserve, FxPoolEvent.post_foreign_reserve, FxPoolEvent.gold_volume,
    ).where(
        FxPoolEvent.pair_id == int(pair_id), FxPoolEvent.id > int(after_id),
        FxPoolEvent.created_at >= start, FxPoolEvent.created_at < end,
    )
    if max_id is not None:
        stmt = stmt.where(FxPoolEvent.id <= int(max_id))
    rows = list((await db.execute(
        stmt.order_by(FxPoolEvent.id).limit(MAX_TAIL_EVENTS + 1)
    )).all())
    if len(rows) > MAX_TAIL_EVENTS:
        raise HTTPException(status_code=503, detail="FX history is catching up; retry shortly",
                            headers={"Retry-After": "5"})
    return rows


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
        # Initial readiness reads may already have cached this ORM state. Both
        # entities must refresh from this statement's snapshot after a flush.
        .execution_options(populate_existing=True)
    )
    state = None
    rows: list[Any] = []
    for state_row, candle_row in (await db.execute(stmt)).all():
        state = state_row
        if candle_row is not None:
            rows.append(candle_row)
    return rows, state


async def _window_max_event_id(db: AsyncSession, pair_id: int,
                               start: datetime, end: datetime) -> int:
    """Max committed pair trade id contributing to ``[start, end)``, or ``0``.

    This is the *coverage* highwater: it is computed over exactly the aligned
    window the chart serves, so an id is never claimed from a trade that is
    outside the window or not committed.  It is deliberately not the
    chronological ``last_event_id`` (ids may skip / be backdated) nor a global
    ``MAX(id)`` (which would over-claim).  Uses the ``(pair_id, created_at)``
    index.
    """
    value = (await db.execute(
        select(func.max(FxPoolEvent.id)).where(
            FxPoolEvent.pair_id == int(pair_id),
            FxPoolEvent.created_at >= start,
            FxPoolEvent.created_at < end,
        )
    )).scalar()
    return int(value or 0)


async def _has_event_in_segment_after(db: AsyncSession, pair_id: int, after_id: int,
                                      start: datetime, end: datetime) -> bool:
    """Indexed EXISTS: any committed trade with ``id > after_id`` in the segment.

    This is the immutable-200 proof for a closed segment: a trade that is
    committed but not covered by ``after_id`` (durable cursor, or the ring's
    applied cursor) could still change the segment, so it must not be cached.
    """
    stmt = (
        select(FxPoolEvent.id)
        .where(
            FxPoolEvent.pair_id == int(pair_id),
            FxPoolEvent.id > int(after_id),
            FxPoolEvent.created_at >= start,
            FxPoolEvent.created_at < end,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first() is not None


def _require_pair(db_pair: FxPair | None, pair_id: int) -> FxPair:
    if db_pair is None:
        raise HTTPException(status_code=404, detail="FX pair not found")
    return db_pair


async def _anchor_event_id(db: AsyncSession, pair_id: int, before: datetime) -> int:
    """Capture the indexed last effective event supplying a window's prior close."""
    value = (await db.execute(select(FxPoolEvent.id).where(
        FxPoolEvent.pair_id == pair_id, FxPoolEvent.created_at < before,
    ).order_by(FxPoolEvent.created_at.desc(), FxPoolEvent.id.desc()).limit(1))).scalar()
    return int(value or 0)


def _chart_window_statement():
    """Reusable SQL structure; request values remain bound parameters."""
    pair_id, start, end = bindparam("chart_pair_id"), bindparam("chart_start"), bindparam("chart_end")
    window = select(func.max(FxPoolEvent.id).label("window_max")).where(
        FxPoolEvent.pair_id == pair_id,
        FxPoolEvent.created_at >= start, FxPoolEvent.created_at < end,
    ).subquery()
    anchor = select(FxPoolEvent.id.label("anchor_id"), FxPoolEvent.kind,
                    FxPoolEvent.pre_gold_reserve, FxPoolEvent.pre_foreign_reserve,
                    FxPoolEvent.post_gold_reserve, FxPoolEvent.post_foreign_reserve).where(
        FxPoolEvent.pair_id == pair_id, FxPoolEvent.created_at < start,
    ).order_by(FxPoolEvent.created_at.desc(), FxPoolEvent.id.desc()).limit(1).subquery()
    return select(
        FxPair.id, FxMarketDataState.history_ready, FxMarketDataState.history_version,
        FxMarketDataState.last_trade_id, FxMarketDataState.last_event_id,
        window.c.window_max, anchor.c.anchor_id, anchor.c.kind,
        anchor.c.pre_gold_reserve, anchor.c.pre_foreign_reserve,
        anchor.c.post_gold_reserve, anchor.c.post_foreign_reserve,
    ).select_from(FxPair).outerjoin(FxMarketDataState, FxMarketDataState.pair_id == FxPair.id).join(
        window, true()).outerjoin(anchor, true()).where(FxPair.id == pair_id)


_CHART_WINDOW_METADATA = _chart_window_statement()


async def _chart_window_bounds(db: AsyncSession, pair_id: int,
                               start: datetime, end: datetime) -> tuple[Any, int, int, Decimal | None]:
    """Pair/state validation, exact coverage and prior close in one statement.

    Call after copying the ring: a newly committed event beyond that copy's
    applied cursor must send the caller to the persisted/tail path. The anchor
    is unbounded here; only use its price when its ID is covered by the ring.
    """
    from app.services.fx.candles import event_prices
    row = (await db.execute(_CHART_WINDOW_METADATA, {
        "chart_pair_id": pair_id, "chart_start": start, "chart_end": end,
    })).first()
    if row is None:
        return None, 0, 0, None
    price = None if row.anchor_id is None else event_prices(row)[1]
    return row, int(row.window_max or 0), int(row.anchor_id or 0), price


async def _history_anchor(db: AsyncSession, pair_id: int, before: datetime,
                          through: int) -> Decimal | None:
    from app.services.fx.candles import event_prices
    row = (await db.execute(select(
        FxPoolEvent.kind, FxPoolEvent.pre_gold_reserve, FxPoolEvent.pre_foreign_reserve,
        FxPoolEvent.post_gold_reserve, FxPoolEvent.post_foreign_reserve,
    ).where(FxPoolEvent.pair_id == pair_id, FxPoolEvent.created_at < before,
            FxPoolEvent.id <= through)
      .order_by(FxPoolEvent.created_at.desc(), FxPoolEvent.id.desc()).limit(1))).first()
    return None if row is None else event_prices(row)[1]


def _fill_buckets(rows: Iterable[dict], start: datetime, end: datetime,
                  seconds: int, anchor: Decimal | None) -> list[dict]:
    """Carry proven historical closes; synthetic rows always have zero events."""
    actual = {row["bucket_start"]: row for row in rows}
    result = []
    at = start
    close = anchor
    while at < end:
        row = actual.get(at)
        if row is not None:
            result.append(row)
            close = row["close_price"]
        elif close is not None:
            result.append({"bucket_start": at, "open_price": close,
                           "high_price": close, "low_price": close, "close_price": close,
                           "gold_volume": Decimal("0"), "n_trades": 0, "n_events": 0})
        at += timedelta(seconds=seconds)
    return result


async def _check_generation(db: AsyncSession, pair_id: int, version: str) -> None:
    current = (await db.execute(select(FxMarketDataState.history_version,
                                      FxMarketDataState.history_ready)
                .where(FxMarketDataState.pair_id == pair_id))).first()
    if current is None or not current.history_ready or str(current.history_version) != version:
        raise HTTPException(status_code=503, detail="FX history generation changed; retry shortly",
                            headers={"Retry-After": "5"})


# ── /chart ──────────────────────────────────────────────────────────────────
async def read_fx_chart_with_meta(db: AsyncSession, pair_id: int, interval: str,
                                  start: datetime, end: datetime) -> tuple[list[dict], int, str]:
    """Chart body with event counts **plus** the exact coverage cursor and generation.

    Returns ``(body, coverage_event_id, history_version)``.  ``coverage`` is the
    max event id contributing to the aligned window or its prior-close anchor,
    so the claimed price changes are represented and no later live delta
    is silently swallowed.  The route exposes it as
    ``X-FX-Through-Event-ID`` / ``X-FX-History-Version``; the body stays a JSON list.

    Bounds are full-bucket provisional semantics: the stored bars and the raw
    tail share the same aligned ``[first_bucket, query_end)`` window, so a
    near-``to`` event is either inside the body (and covered) or outside it (and
    left to the live delta).  Validation (range, interval, output and fine
    source bucket ceiling) happens before any query.  Missing metadata is an
    explicit 503, never a fabricated coverage value.
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
    # Aligned, full-bucket served window shared by stored bars, raw tail and
    # coverage.  ``plan.base_step`` is the fine stored interval (== the output
    # interval for native periods), so a legacy rollup is bounded on its source
    # scan too and cannot fan a small output into millions of ORM rows.
    first_bucket = _bucket_start(start, plan.seconds)
    last_bucket = _bucket_start(end - timedelta(microseconds=1), plan.seconds)
    query_end = last_bucket + timedelta(seconds=plan.seconds)
    source_buckets = int(math.ceil((query_end - first_bucket).total_seconds() / plan.base_step))
    if source_buckets > MAX_CHART_BUCKETS:
        raise HTTPException(
            status_code=422,
            detail=f"chart source window exceeds {MAX_CHART_BUCKETS} fine buckets",
        )

    pair_id = int(pair_id)
    # Copy rows and cursor synchronously before the SQL coverage snapshot.
    # This preserves the post-copy commit detection while combining metadata
    # roundtrips; projected fields also bypass stale identity-map state.
    ring_rows = rt = None
    if plan.native is not None:
        ring_rows = FX_MARKET_DATA.get_candles(pair_id, plan.native, first_bucket, query_end)
        rt = FX_MARKET_DATA.state(pair_id)
    state, window_max, anchor_id, anchor = await _chart_window_bounds(
        db, pair_id, first_bucket, query_end)
    if state is None:
        raise HTTPException(status_code=404, detail="FX pair not found")
    if not state.history_ready or (state.last_trade_id > 0 and state.last_event_id == 0):
        # No complete history yet: retryable, never a full raw-trade rebuild.
        raise HTTPException(status_code=503, detail="FX history is not ready")
    version = str(state.history_version)

    if plan.native is not None:
        if (ring_rows is not None and rt is not None and rt.get("history_ready")
                and str(rt.get("history_version")) == version):
            # Exact committed bounds must fit the copied ring's applied cursor.
            if max(window_max, anchor_id) <= int(rt.get("applied_event_id") or 0):
                coverage = max(window_max, anchor_id)
                await _check_generation(db, pair_id, version)
                filled = _fill_buckets(ring_rows, first_bucket, query_end, plan.seconds, anchor)
                return ([_chart_row(plan.interval, row) for row in filled], coverage, version)


    # Persisted base candles + the persisted state in ONE statement, then only
    # the projected unflushed tail above the durable cursor capped at the
    # window's committed max.  A flush that commits mid-request can only appear
    # in the tail.
    base_interval = plan.base or plan.native or plan.interval
    base_rows, db_state = await _load_candles_with_state(
        db, pair_id, base_interval, first_bucket, query_end,
    )
    if db_state is None or not db_state.history_ready or (db_state.last_trade_id > 0 and db_state.last_event_id == 0):
        raise HTTPException(status_code=503, detail="FX history is not ready")
    # The generation travels with the candle rows from the same statement, so a
    # rebuild in flight can never mix two generations in one body.
    db_version = str(db_state.history_version)
    durable = int(db_state.last_event_id)
    coverage = max(await _anchor_event_id(db, pair_id, first_bucket),
                   await _window_max_event_id(db, pair_id, first_bucket, query_end))

    merged: dict[datetime, dict] = {}
    for row in base_rows:
        agg = _aggregate_bucket(_row_to_agg(row), plan.seconds)
        existing = merged.get(agg["bucket_start"])
        merged[agg["bucket_start"]] = agg if existing is None else merge_row(existing, agg)

    tail_events = await _load_tail_events(
        db, pair_id, durable, first_bucket, query_end, max_id=coverage)
    for row in compute_fx_candle_rows(tail_events):
        if row["interval"] != base_interval:
            continue
        agg = _aggregate_bucket(row, plan.seconds)
        existing = merged.get(agg["bucket_start"])
        merged[agg["bucket_start"]] = agg if existing is None else merge_row(existing, agg)

    anchor = await _history_anchor(db, pair_id, first_bucket, max(durable, coverage))
    await _check_generation(db, pair_id, db_version)
    filled = _fill_buckets(merged.values(), first_bucket, query_end, plan.seconds, anchor)
    return ([_chart_row(plan.interval, row) for row in filled], coverage, db_version)


def _aggregate_bucket(row: dict, seconds: int) -> dict:
    """Align a fine row's ``bucket_start`` to the requested output bucket.

    Without this the merged chart point would report the *first fine row's*
    timestamp (e.g. 12:03 for an off-boundary 5m rollup) instead of the aligned
    bucket start the legacy chart contract uses.
    """
    bucket = _bucket_start(row["bucket_start"], seconds)
    if row["bucket_start"] == bucket:
        return row
    return {**row, "bucket_start": bucket}


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
        "events": [],
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
        out["events"].append(int(row["n_events"]))
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
    if state is None or not state.history_ready or (state.last_trade_id > 0 and state.last_event_id == 0):
        raise HTTPException(status_code=503, detail="FX 历史未就绪，请稍后重试")
    if str(state.history_version) != str(history_version):
        # The URL names a generation that is no longer current: the segment can
        # never be served again, and must not be mixed into the new history.
        raise HTTPException(status_code=404, detail="历史版本已失效")

    # Cache key is product + pair + history generation + period + segment.
    key = ("fx", pair_id, str(history_version), interval, int(segment_epoch))
    cached = _lru_get(key)
    if cached is not None:
        start_dt = datetime.fromtimestamp(segment_epoch, tz=UTC)
        end_dt = datetime.fromtimestamp(seg_end, tz=UTC)
        if await _has_event_in_segment_after(db, pair_id, int(state.last_event_id), start_dt, end_dt):
            _lru().pop(key, None)
        else:
            return cached

    # Cache miss: take a short shared lock on the pair row for the proof + body.
    # Every producer takes ``FOR UPDATE`` on this row *before* constructing its
    # ``FxTrade`` and holds it until commit, so the shared lock waits out any
    # in-flight producer whose trade was born before ``seg_end``; once held, no
    # such transaction can commit between the ``EXISTS`` proof and the candle
    # load.  The lock is held only for this short read transaction and released
    # (``commit``) before the response is serialised/returned.
    try:
        await db.execute(
            select(FxPair.id).where(FxPair.id == pair_id).with_for_update(read=True)
        )
    except SQLAlchemyError as exc:
        # A true SQL read-only / standby connection cannot take the lock.  A
        # non-cache 503 (client falls back to lightweight /chart) is safer than
        # poisoning the immutable cache with an unproven segment.
        raise HTTPException(
            status_code=503, detail="历史段一致性锁不可用；请稍后重试或使用图表回退"
        ) from exc

    # Re-read under the lock: the generation may have rotated while waiting.
    state = await _read_state(db, pair_id)
    if state is None or not state.history_ready or (state.last_trade_id > 0 and state.last_event_id == 0):
        raise HTTPException(status_code=503, detail="FX 历史未就绪，请稍后重试")
    if str(state.history_version) != str(history_version):
        raise HTTPException(status_code=404, detail="历史版本已失效")

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
            applied = int(rt.get("applied_event_id") or 0)
            if not await _has_event_in_segment_after(db, pair_id, applied,
                                                     seg_start_dt, seg_end_dt):
                anchor = await _history_anchor(db, pair_id, seg_start_dt, applied)
                filled = _fill_buckets(ring_rows, seg_start_dt, seg_end_dt, tier.step, anchor)
                enc = _encode_segment(filled, interval, segment_epoch, seg_end)
                await db.commit()
                _lru_put(key, enc)
                return enc

    # Defence 3: persisted candles.  Refuse while a queued FX flush or a
    # committed trade past the durable cursor could still change the segment.
    pending = FX_MARKET_DATA.flusher.oldest_pending_bucket(pair_id)
    if pending is not None and _utc(pending) < seg_end_dt:
        raise HTTPException(status_code=503, detail="段落库未完成，请稍后重试")
    durable = int(state.last_event_id)
    if await _has_event_in_segment_after(db, pair_id, durable, seg_start_dt, seg_end_dt):
        raise HTTPException(status_code=503, detail="段落库未完成，请稍后重试")

    rows = await _load_candles(db, pair_id, interval, seg_start_dt, seg_end_dt)
    anchor = await _history_anchor(db, pair_id, seg_start_dt, durable)
    filled = _fill_buckets((_row_to_agg(row) for row in rows), seg_start_dt, seg_end_dt,
                           tier.step, anchor)
    enc = _encode_segment(filled, interval, segment_epoch, seg_end)
    await db.commit()
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
        "history_ready": bool(state.history_ready) and not (state.last_trade_id > 0 and state.last_event_id == 0),
    }


# ── rolling 24h gold volume ─────────────────────────────────────────────────
def _gold_sum_expr():
    return func.coalesce(
        func.sum(case((FxTrade.side == "buy", FxTrade.input_amount),
                      else_=FxTrade.output_amount)),
        Decimal("0"),
    )


async def fx_volume_24h(db: AsyncSession, pair_id: int, now: datetime) -> Decimal | None:
    """Exact rolling-24h gold-side volume, or ``None`` when history is not ready.

    ``None`` lets the caller keep the exact SQL ``SUM`` fallback.  When ready,
    the durable cursor, the fully-covered minute-candle aggregate and the raw
    sums for the two partial edges and the outstanding tail are all read in a
    **single SQL statement**, so they share one snapshot.  Reading the cursor
    separately from the candle sum could otherwise count zero for a batch that a
    concurrent flush commits between the two observations.
    """
    now = _utc(now)
    since = now - timedelta(hours=24)
    b_start = _ceil_bucket(since, 60)
    b_end = _bucket_start(now, 60)
    if b_start > b_end:
        # Window shorter than a minute (or an unaligned tiny window): everything
        # is a partial range.
        b_start = b_end

    pid = int(pair_id)
    durable_sq = (
        select(FxMarketDataState.last_event_id)
        .where(FxMarketDataState.pair_id == pid)
        .scalar_subquery()
    )
    ready_sq = (
        select(FxMarketDataState.history_ready)
        .where(FxMarketDataState.pair_id == pid)
        .scalar_subquery()
    )
    candle_sq = (
        select(func.coalesce(func.sum(FxCandle.gold_volume), Decimal("0")))
        .where(
            FxCandle.pair_id == pid,
            FxCandle.interval == "1m",
            FxCandle.bucket_start >= b_start,
            FxCandle.bucket_start < b_end,
        )
        .scalar_subquery()
    )
    # Disjoint raw ranges: the boundary minutes around the full-minute band and
    # the outstanding tail above the durable cursor inside that band.
    head_sq = (
        select(_gold_sum_expr())
        .where(FxTrade.pair_id == pid, FxTrade.created_at >= since,
               FxTrade.created_at < b_start)
        .scalar_subquery()
    )
    edge_sq = (
        select(_gold_sum_expr())
        .where(FxTrade.pair_id == pid, FxTrade.created_at >= b_end,
               FxTrade.created_at < now)
        .scalar_subquery()
    )
    tail_sq = (
        select(func.coalesce(func.sum(FxPoolEvent.gold_volume), Decimal("0")))
        .where(
            FxPoolEvent.pair_id == pid,
            FxPoolEvent.created_at >= b_start,
            FxPoolEvent.created_at < b_end,
            FxPoolEvent.id > durable_sq,
        )
        .scalar_subquery()
    )

    row = (await db.execute(select(ready_sq, candle_sq, head_sq, edge_sq, tail_sq))).one()
    ready = row[0]
    if ready is None or not bool(ready):
        return None
    total = Decimal("0")
    for value in row[1:]:
        total += Decimal(value) if value is not None else Decimal("0")
    return total


async def read_fx_stream_history(db: AsyncSession, pair_id: int, now: datetime) -> dict:
    """Bounded persisted/ring tail at the caller's locked snapshot boundary.

    The caller holds the pair shared lock while reading quote + history. No
    catch-up, creation, or flushing is done from the streaming GET.
    """
    from app.services.history_ring import seal_boundary
    now = _utc(now)
    epoch = int(now.timestamp())
    segments = {}
    version = None
    for interval, tier in RING_SPEC.items():
        t0 = seal_boundary(interval, epoch)
        end = epoch - epoch % tier.step + tier.step
        chart, _through, current_version = await read_fx_chart_with_meta(
            db, pair_id, interval, datetime.fromtimestamp(t0, tz=UTC),
            datetime.fromtimestamp(end, tz=UTC))
        if version is not None and version != current_version:
            raise HTTPException(status_code=503, detail="FX history generation changed; retry shortly")
        version = current_version
        rows = [{"bucket_start": point["bucket_start"],
                 "open_price": point["open"], "high_price": point["high"],
                 "low_price": point["low"], "close_price": point["close"],
                 "gold_volume": point["volume"], "n_trades": point["n_trades"],
                 "n_events": point["n_events"]} for point in chart]
        segments[interval] = _encode_segment(rows, interval, t0, end)
    cursor = (await db.execute(select(func.max(FxPoolEvent.id))
              .where(FxPoolEvent.pair_id == pair_id))).scalar() or 0
    await _check_generation(db, pair_id, str(version))
    return {"history_version": version, "history_ready": True,
            "history_tail": segments, "history_tail_at": now.isoformat(),
            "history_tail_through_event_id": int(cursor)}
