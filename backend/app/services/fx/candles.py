"""Event OHLCV aggregation and atomic, idempotent candle checkpoint writes.

Input projections expose id, pair_id, created_at, kind, pre/post reserves and
 gold_volume. FxTrade IDs remain source references, never event cursors.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Iterable, Sequence
from uuid import uuid4

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fx import FxCandle, FxMarketDataState

# Same four periods as the LMSR candle pipeline; existing FX charts use 1m/15m/1h.
FX_CANDLE_INTERVALS: tuple[tuple[str, int], ...] = (
    ("10s", 10),
    ("1m", 60),
    ("15m", 900),
    ("1h", 3600),
)


class FxCandleCursorConflict(RuntimeError):
    """The persisted cursor disagrees with the batch's expected cursor.

    Raised instead of silently applying rows to the wrong range: the caller
    must re-read the durable state and recompute the batch.
    """


def new_history_version() -> str:
    """A fresh opaque history generation id (UUID4 string)."""
    return str(uuid4())


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bucket_start(ts: datetime, step_seconds: int) -> datetime:
    """UTC bucket start, floored on epoch seconds (matches the chart code)."""
    ts = _utc(ts)
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % step_seconds), tz=timezone.utc)


def _order_key(at: datetime, event_id: int) -> tuple[datetime, int]:
    """``(created_at, id)`` ordering key, tolerant of naive SQLite datetimes."""
    return (_utc(at), int(event_id))


def _get(event: Any, name: str) -> Any:
    """Read a projected trade field from a Row/namespace or a plain mapping."""
    if isinstance(event, dict):
        return event[name]
    return getattr(event, name)


def event_prices(event: Any) -> tuple[Decimal, Decimal]:
    """Six-place marginal prices from the immutable event reserve snapshot."""
    from app.services.fx.amm import marginal_price
    post = marginal_price(Decimal(_get(event, "post_gold_reserve")),
                          Decimal(_get(event, "post_foreign_reserve"))).quantize(
                              Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
    if _get(event, "kind") == "initialize":
        return post, post
    pre = marginal_price(Decimal(_get(event, "pre_gold_reserve")),
                         Decimal(_get(event, "pre_foreign_reserve"))).quantize(
                             Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
    return pre, post


def compute_fx_candle_rows(events: Iterable[Any]) -> list[dict]:
    """Fold pool events into one row per ``(pair_id, interval, bucket_start)``.

    Buckets are UTC; open is the first pre-event price, close the last post-event
    price, and high/low include both, ordered by ``(created_at, id)``;
    ``gold_volume`` is stored on the event; only kind=trade increments n_trades.  Each row
    carries the first/last ordering keys so later batches can merge without
    re-reading history.
    """
    buckets: dict[tuple[int, str, datetime], dict] = {}

    for event in events:
        pair_id = int(_get(event, "pair_id"))
        pre_price, price = event_prices(event)
        for interval, step in FX_CANDLE_INTERVALS:
            ts = _utc(_get(event, "created_at"))
            event_id = int(_get(event, "id"))
            volume = Decimal(_get(event, "gold_volume"))
            n_trades = int(_get(event, "kind") == "trade")
            bucket_start = _bucket_start(ts, step)
            key = (pair_id, interval, bucket_start)
            row = buckets.get(key)
            if row is None:
                buckets[key] = {
                    "pair_id": pair_id,
                    "interval": interval,
                    "bucket_start": bucket_start,
                    "open_price": pre_price,
                    "high_price": max(pre_price, price),
                    "low_price": min(pre_price, price),
                    "close_price": price,
                    "gold_volume": volume,
                    "n_trades": n_trades,
                    "n_events": 1,
                    "first_event_at": ts,
                    "first_event_id": event_id,
                    "last_event_at": ts,
                    "last_event_id": event_id,
                }
                continue

            if _order_key(ts, event_id) < _order_key(row["first_event_at"], row["first_event_id"]):
                row["open_price"] = pre_price
                row["first_event_at"] = ts
                row["first_event_id"] = event_id
            if _order_key(ts, event_id) > _order_key(row["last_event_at"], row["last_event_id"]):
                row["close_price"] = price
                row["last_event_at"] = ts
                row["last_event_id"] = event_id
            row["high_price"] = max(row["high_price"], pre_price, price)
            row["low_price"] = min(row["low_price"], pre_price, price)
            row["gold_volume"] += volume
            row["n_trades"] += n_trades
            row["n_events"] += 1

    interval_rank = {name: rank for rank, (name, _) in enumerate(FX_CANDLE_INTERVALS)}
    return sorted(
        buckets.values(),
        key=lambda row: (row["pair_id"], interval_rank[row["interval"]], row["bucket_start"]),
    )


def merge_row(a: dict, b: dict) -> dict:
    """Order-aware merge of two rows for the same candle key.

    The result keeps the earlier ``open``/``first`` and later ``close``/``last``
    keys, takes high/low extremes, and sums volume/trade count.
    """
    first = a if _order_key(a["first_event_at"], a["first_event_id"]) <= _order_key(b["first_event_at"], b["first_event_id"]) else b
    last = a if _order_key(a["last_event_at"], a["last_event_id"]) >= _order_key(b["last_event_at"], b["last_event_id"]) else b
    return {
        "pair_id": first["pair_id"],
        "interval": first["interval"],
        "bucket_start": first["bucket_start"],
        "open_price": first["open_price"],
        "close_price": last["close_price"],
        "high_price": max(a["high_price"], b["high_price"]),
        "low_price": min(a["low_price"], b["low_price"]),
        "gold_volume": a["gold_volume"] + b["gold_volume"],
        "n_trades": int(a["n_trades"]) + int(b["n_trades"]),
        "n_events": int(a["n_events"]) + int(b["n_events"]),
        "first_event_at": first["first_event_at"],
        "first_event_id": first["first_event_id"],
        "last_event_at": last["last_event_at"],
        "last_event_id": last["last_event_id"],
    }


def _earlier_sql(earlier_at, earlier_id, later_at, later_id):
    """SQL predicate: ``(earlier_at, earlier_id) < (later_at, later_id)``."""
    return or_(
        earlier_at < later_at,
        and_(earlier_at == later_at, earlier_id < later_id),
    )


async def _upsert_candles(db: AsyncSession, rows: Sequence[dict]) -> None:
    """Order-aware multi-row UPSERT; each event's volume is added exactly once.

    ``open``/``close`` and the first/last ordering keys only move when the
    incoming row is genuinely earlier/later, so an out-of-order batch still
    produces the true bucket open/close.  ``gold_volume``/``n_trades`` add.
    """
    rows_list = list(rows)
    if not rows_list:
        return

    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        insert_fn = pg_insert
        greatest, least = func.greatest, func.least
    elif dialect == "sqlite":
        insert_fn = sqlite_insert
        # SQLite has no GREATEST/LEAST; scalar MAX/MIN match the two-arg use.
        greatest, least = func.max, func.min
    else:
        raise NotImplementedError(f"unsupported DB dialect for FX candles: {dialect}")

    stmt = insert_fn(FxCandle).values(rows_list)
    ins = stmt.excluded
    incoming_earlier = _earlier_sql(ins.first_event_at, ins.first_event_id, FxCandle.first_event_at, FxCandle.first_event_id)
    incoming_later = _earlier_sql(FxCandle.last_event_at, FxCandle.last_event_id, ins.last_event_at, ins.last_event_id)

    stmt = stmt.on_conflict_do_update(
        index_elements=["pair_id", "interval", "bucket_start"],
        set_={
            "open_price": case((incoming_earlier, ins.open_price), else_=FxCandle.open_price),
            "high_price": greatest(FxCandle.high_price, ins.high_price),
            "low_price": least(FxCandle.low_price, ins.low_price),
            "close_price": case((incoming_later, ins.close_price), else_=FxCandle.close_price),
            "gold_volume": FxCandle.gold_volume + ins.gold_volume,
            "n_trades": FxCandle.n_trades + ins.n_trades,
            "n_events": FxCandle.n_events + ins.n_events,
            "first_event_at": case((incoming_earlier, ins.first_event_at), else_=FxCandle.first_event_at),
            "first_event_id": case((incoming_earlier, ins.first_event_id), else_=FxCandle.first_event_id),
            "last_event_at": case((incoming_later, ins.last_event_at), else_=FxCandle.last_event_at),
            "last_event_id": case((incoming_later, ins.last_event_id), else_=FxCandle.last_event_id),
            "updated_at": func.now(),
        },
    )
    await db.execute(stmt)


async def _select_state(db: AsyncSession, pair_id: int, *, for_update: bool) -> FxMarketDataState | None:
    stmt = select(FxMarketDataState).where(FxMarketDataState.pair_id == pair_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalars().first()


async def _insert_state_if_absent(
    db: AsyncSession, pair_id: int, *, history_version: str, history_ready: bool,
) -> bool:
    """Insert the pair state if absent; return True when this call inserted it.

    ``ON CONFLICT DO NOTHING`` makes a concurrent creator wait for the winning
    transaction instead of raising, and ``RETURNING`` says whether ours won.
    """
    values = {
        "pair_id": pair_id,
        "last_event_id": 0,
        "history_version": history_version,
        "history_ready": history_ready,
        "updated_at": datetime.now(timezone.utc),
    }
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        stmt = pg_insert(FxMarketDataState).values(**values)
    elif dialect == "sqlite":
        stmt = sqlite_insert(FxMarketDataState).values(**values)
    else:
        raise NotImplementedError(f"unsupported DB dialect for FX market state: {dialect}")
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["pair_id"],
    ).returning(FxMarketDataState.pair_id)
    return (await db.execute(stmt)).first() is not None


async def load_market_data_state(db: AsyncSession, pair_id: int) -> FxMarketDataState:
    """Return the pair's durable state, creating it inside the caller's txn.

    Never commits.  The caller must own a writable transaction: shared read
    paths (which may run on a read-only instance) must not call this; they
    should read the state (and treat a missing row as "not ready") instead.
    """
    state = await _select_state(db, pair_id, for_update=False)
    if state is None:
        await _insert_state_if_absent(
            db, pair_id, history_version=new_history_version(), history_ready=False,
        )
        state = await _select_state(db, pair_id, for_update=False)
        if state is None:
            raise FxCandleCursorConflict(f"pair {pair_id} state row disappeared during creation")
    return state


async def apply_candle_batch(
    db: AsyncSession,
    *,
    pair_id: int,
    expected_event_id: int,
    through_event_id: int,
    rows: Sequence[dict],
    history_version: str,
    history_ready: bool | None = None,
) -> bool:
    """Atomically persist ``(expected_event_id, through_event_id]`` candles.

    The caller owns the transaction; this function never commits.  It locks the
    per-pair state row, then:

    * rejects a stale generation: an existing state whose ``history_version``
      differs from the requested one raises :class:`FxCandleCursorConflict`,
      **including when the cursor already equals** ``through_event_id``.  Only
      first creation adopts the requested generation; a regular apply can never
      silently change an existing generation.
    * if the persisted cursor is already at ``through_event_id``:
      - volume-bearing duplicates are never re-added; only an explicit
        ``history_ready`` transition is applied (returning whether it changed);
      - an empty metadata-only call (``expected == through``, no rows) is the
        intended way to mark initial-backfill or rebuild completion.
    * raises :class:`FxCandleCursorConflict` when the persisted cursor is
      neither the expected one nor already through.

    ``history_ready=None`` preserves the existing flag (``False`` on creation);
    a bool sets it.  ``rows`` are the output of :func:`compute_fx_candle_rows`
    and must all belong to ``pair_id``.
    """
    if through_event_id < expected_event_id:
        raise ValueError(
            f"through_event_id {through_event_id} < expected_event_id {expected_event_id}"
        )
    rows_list = list(rows)
    if through_event_id == expected_event_id and rows_list:
        raise ValueError(
            f"pair {pair_id} batch ({expected_event_id}, {through_event_id}] "
            "consumes no event but carries candle rows"
        )
    mismatched = [row for row in rows_list if int(row["pair_id"]) != pair_id]
    if mismatched:
        raise ValueError(f"batch rows do not belong to pair {pair_id}: {mismatched[:1]}")

    state = await _select_state(db, pair_id, for_update=True)
    created = False
    if state is None:
        if expected_event_id != 0:
            raise FxCandleCursorConflict(
                f"pair {pair_id} has no durable cursor but caller expected "
                f"{expected_event_id} (through {through_event_id})"
            )
        created = await _insert_state_if_absent(
            db, pair_id, history_version=history_version,
            history_ready=False if history_ready is None else history_ready,
        )
        state = await _select_state(db, pair_id, for_update=True)
        if state is None:
            raise FxCandleCursorConflict(
                f"pair {pair_id} state row disappeared during creation"
            )

    if state.history_version != history_version:
        raise FxCandleCursorConflict(
            f"pair {pair_id} history generation is {state.history_version!r}, "
            f"caller requested {history_version!r} (missing {len(rows_list)} rows "
            f"through {through_event_id})"
        )

    if not created and state.last_event_id == through_event_id:
        # Volume (if any) is already durable: never re-add it.  Only an explicit
        # readiness transition may still be applied.
        if history_ready is None or history_ready == state.history_ready:
            return False
        state.history_ready = history_ready
        state.updated_at = datetime.now(timezone.utc)
        return True
    if state.last_event_id != expected_event_id:
        raise FxCandleCursorConflict(
            f"pair {pair_id} durable cursor is {state.last_event_id}, caller "
            f"expected {expected_event_id} (through {through_event_id})"
        )

    if rows_list:
        await _upsert_candles(db, rows_list)

    state.last_event_id = through_event_id
    if history_ready is not None:
        state.history_ready = history_ready
    state.updated_at = datetime.now(timezone.utc)
    return True
