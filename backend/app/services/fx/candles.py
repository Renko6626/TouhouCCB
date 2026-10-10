"""FX candle aggregation and durable batch application.

Two responsibilities live here:

* :func:`compute_fx_candle_rows` is pure: it turns any iterable of trade-like
  objects (real ``FxTrade`` rows or projected rows exposing ``id``, ``pair_id``,
  ``created_at``, ``post_price``, ``side``, ``input_amount``, ``output_amount``)
  into 4-interval candle rows.  It never touches the ORM.
* :func:`apply_candle_batch` / :func:`load_market_data_state` own persistence:
  candle upsert, durable cursor and history version move in the caller's
  transaction, and a duplicate batch is detected by the persisted cursor so an
  uncertain commit is never re-added.

The runtime (ring, notifications, retry loop) is a separate module; this layer
only freezes the storage contract.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
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


def _order_key(at: datetime, trade_id: int) -> tuple[datetime, int]:
    """``(created_at, id)`` ordering key, tolerant of naive SQLite datetimes."""
    return (_utc(at), int(trade_id))


def _get(trade: Any, name: str) -> Any:
    """Read a projected trade field from a Row/namespace or a plain mapping."""
    if isinstance(trade, dict):
        return trade[name]
    return getattr(trade, name)


def _gold_volume(trade: Any) -> Decimal:
    """buy → gold in (input), sell → gold out (output), matching the snapshot."""
    amount = _get(trade, "input_amount") if _get(trade, "side") == "buy" else _get(trade, "output_amount")
    return Decimal(amount)


def compute_fx_candle_rows(trades: Iterable[Any]) -> list[dict]:
    """Fold trades into one row per ``(pair_id, interval, bucket_start)``.

    Buckets are UTC; OHLC is ``post_price`` taken in ``(created_at, id)`` order;
    ``gold_volume`` is buy ``input_amount`` / sell ``output_amount``.  Each row
    carries the first/last ordering keys so later batches can merge without
    re-reading history.
    """
    buckets: dict[tuple[int, str, datetime], dict] = {}

    for trade in trades:
        pair_id = int(_get(trade, "pair_id"))
        for interval, step in FX_CANDLE_INTERVALS:
            ts = _utc(_get(trade, "created_at"))
            trade_id = int(_get(trade, "id"))
            price = Decimal(_get(trade, "post_price"))
            volume = _gold_volume(trade)
            bucket_start = _bucket_start(ts, step)
            key = (pair_id, interval, bucket_start)
            row = buckets.get(key)
            if row is None:
                buckets[key] = {
                    "pair_id": pair_id,
                    "interval": interval,
                    "bucket_start": bucket_start,
                    "open_price": price,
                    "high_price": price,
                    "low_price": price,
                    "close_price": price,
                    "gold_volume": volume,
                    "n_trades": 1,
                    "first_trade_at": ts,
                    "first_trade_id": trade_id,
                    "last_trade_at": ts,
                    "last_trade_id": trade_id,
                }
                continue

            if _order_key(ts, trade_id) < _order_key(row["first_trade_at"], row["first_trade_id"]):
                row["open_price"] = price
                row["first_trade_at"] = ts
                row["first_trade_id"] = trade_id
            if _order_key(ts, trade_id) > _order_key(row["last_trade_at"], row["last_trade_id"]):
                row["close_price"] = price
                row["last_trade_at"] = ts
                row["last_trade_id"] = trade_id
            row["high_price"] = max(row["high_price"], price)
            row["low_price"] = min(row["low_price"], price)
            row["gold_volume"] += volume
            row["n_trades"] += 1

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
    first = a if _order_key(a["first_trade_at"], a["first_trade_id"]) <= _order_key(b["first_trade_at"], b["first_trade_id"]) else b
    last = a if _order_key(a["last_trade_at"], a["last_trade_id"]) >= _order_key(b["last_trade_at"], b["last_trade_id"]) else b
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
        "first_trade_at": first["first_trade_at"],
        "first_trade_id": first["first_trade_id"],
        "last_trade_at": last["last_trade_at"],
        "last_trade_id": last["last_trade_id"],
    }


def _earlier_sql(earlier_at, earlier_id, later_at, later_id):
    """SQL predicate: ``(earlier_at, earlier_id) < (later_at, later_id)``."""
    return or_(
        earlier_at < later_at,
        and_(earlier_at == later_at, earlier_id < later_id),
    )


async def _upsert_candles(db: AsyncSession, rows: Sequence[dict]) -> None:
    """Order-aware multi-row UPSERT; each trade's volume is added exactly once.

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
    incoming_earlier = _earlier_sql(ins.first_trade_at, ins.first_trade_id, FxCandle.first_trade_at, FxCandle.first_trade_id)
    incoming_later = _earlier_sql(FxCandle.last_trade_at, FxCandle.last_trade_id, ins.last_trade_at, ins.last_trade_id)

    stmt = stmt.on_conflict_do_update(
        index_elements=["pair_id", "interval", "bucket_start"],
        set_={
            "open_price": case((incoming_earlier, ins.open_price), else_=FxCandle.open_price),
            "high_price": greatest(FxCandle.high_price, ins.high_price),
            "low_price": least(FxCandle.low_price, ins.low_price),
            "close_price": case((incoming_later, ins.close_price), else_=FxCandle.close_price),
            "gold_volume": FxCandle.gold_volume + ins.gold_volume,
            "n_trades": FxCandle.n_trades + ins.n_trades,
            "first_trade_at": case((incoming_earlier, ins.first_trade_at), else_=FxCandle.first_trade_at),
            "first_trade_id": case((incoming_earlier, ins.first_trade_id), else_=FxCandle.first_trade_id),
            "last_trade_at": case((incoming_later, ins.last_trade_at), else_=FxCandle.last_trade_at),
            "last_trade_id": case((incoming_later, ins.last_trade_id), else_=FxCandle.last_trade_id),
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
        "last_trade_id": 0,
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
    expected_trade_id: int,
    through_trade_id: int,
    rows: Sequence[dict],
    history_version: str,
    history_ready: bool | None = None,
) -> bool:
    """Atomically persist ``(expected_trade_id, through_trade_id]`` candles.

    The caller owns the transaction; this function never commits.  It locks the
    per-pair state row, then:

    * rejects a stale generation: an existing state whose ``history_version``
      differs from the requested one raises :class:`FxCandleCursorConflict`,
      **including when the cursor already equals** ``through_trade_id``.  Only
      first creation adopts the requested generation; a regular apply can never
      silently change an existing generation.
    * if the persisted cursor is already at ``through_trade_id``:
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
    if through_trade_id < expected_trade_id:
        raise ValueError(
            f"through_trade_id {through_trade_id} < expected_trade_id {expected_trade_id}"
        )
    rows_list = list(rows)
    if through_trade_id == expected_trade_id and rows_list:
        raise ValueError(
            f"pair {pair_id} batch ({expected_trade_id}, {through_trade_id}] "
            "consumes no trade but carries candle rows"
        )
    mismatched = [row for row in rows_list if int(row["pair_id"]) != pair_id]
    if mismatched:
        raise ValueError(f"batch rows do not belong to pair {pair_id}: {mismatched[:1]}")

    state = await _select_state(db, pair_id, for_update=True)
    created = False
    if state is None:
        if expected_trade_id != 0:
            raise FxCandleCursorConflict(
                f"pair {pair_id} has no durable cursor but caller expected "
                f"{expected_trade_id} (through {through_trade_id})"
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
            f"through {through_trade_id})"
        )

    if not created and state.last_trade_id == through_trade_id:
        # Volume (if any) is already durable: never re-add it.  Only an explicit
        # readiness transition may still be applied.
        if history_ready is None or history_ready == state.history_ready:
            return False
        state.history_ready = history_ready
        state.updated_at = datetime.now(timezone.utc)
        return True
    if state.last_trade_id != expected_trade_id:
        raise FxCandleCursorConflict(
            f"pair {pair_id} durable cursor is {state.last_trade_id}, caller "
            f"expected {expected_trade_id} (through {through_trade_id})"
        )

    if rows_list:
        await _upsert_candles(db, rows_list)

    state.last_trade_id = through_trade_id
    if history_ready is not None:
        state.history_ready = history_ready
    state.updated_at = datetime.now(timezone.utc)
    return True
