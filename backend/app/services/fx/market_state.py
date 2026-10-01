"""FX incremental market-data runtime: ring, per-pair cursors, tail and deltas.

This module is the runtime half of the plan's Task 2/3 for FX.  ``FxTrade``
stays the source of truth; this service incrementally folds committed trades
into a bounded in-memory ring, queues reliable candle batches for the durable
storage layer (``candles`` / ``candle_flusher``) and exposes read-only snapshots
for the history/SSE read paths.

Hard constraints this module honours:

* Candle persistence is only ever done by :mod:`app.services.fx.candles`; the
  durable cursor and the candle rows move in the *same* transaction.  This
  service never advances a durable cursor itself and never re-adds volume after
  an uncertain flush (the persisted checkpoint decides).
* Consumption is serialised per pair by an in-process ``asyncio.Lock``.  It is
  deliberately **not** an economic/global lock and never touches finance GATES:
  the quote/credit path must not be able to read or wait on this cache.
* Readers (``write_owner=False``) only warm from persisted states/candles and
  never create states, flush or run a background consumer.
* The public trade buffer is bounded and discardable; it is never used as the
  source for candle aggregation.
* No network awaits and no financial mutations.

The storage contract is frozen in ``task-2-storage-report.md``.  One gap is
worth calling out explicitly: :meth:`FxCandleFlusher.add_batch` has no
``history_ready`` argument and the flusher always applies batches with the
``apply_candle_batch`` default ``history_ready=True``.  To keep the requirement
"readiness true only when the original cutoff is covered" the runtime performs
the one-time initial backfill with its own ``apply_candle_batch`` calls
(``history_ready=False`` for intermediate pages) and flips readiness explicitly;
see :meth:`FxMarketDataService._ensure_ready`.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import func, select

from app.core.database import async_session_maker
from app.models.fx import FxCandle, FxMarketDataState, FxPair, FxTrade
from app.services.fx.candle_flusher import FxCandleFlusher
from app.services.fx.candles import (
    FxCandleCursorConflict,
    apply_candle_batch,
    compute_fx_candle_rows,
    merge_row,
    new_history_version,
)
from app.services.history_ring import RING_SPEC, seal_boundary

logger = logging.getLogger(__name__)

#: Full per-pair max-id reconciliation cadence (seconds); notifications are an
#: accelerator on top of this.
RECONCILE_INTERVAL = 5.0

#: Bounded discardable public trade buffer per pair.  Overflow sets
#: ``history_invalidated`` and the next drain drops the partial buffer; the
#: consumer must refetch the tail instead of trusting a partial delta list.
PUBLIC_TRADE_BUFFER = 512

UTC = timezone.utc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _candle_to_row(candle: FxCandle, bucket_start: datetime | None = None) -> dict:
    """Adapt a persisted ``FxCandle`` ORM row to the pure aggregation row shape.

    Keeping the exact ``compute_fx_candle_rows``/``merge_row`` keys is what makes
    a warm-loaded bucket mergeable with a freshly computed one without a second
    codec.
    """
    return {
        "pair_id": int(candle.pair_id),
        "interval": candle.interval,
        "bucket_start": _utc(bucket_start or candle.bucket_start),
        "open_price": _dec(candle.open_price),
        "high_price": _dec(candle.high_price),
        "low_price": _dec(candle.low_price),
        "close_price": _dec(candle.close_price),
        "gold_volume": _dec(candle.gold_volume),
        "n_trades": int(candle.n_trades),
        "first_trade_at": _utc(candle.first_trade_at),
        "first_trade_id": int(candle.first_trade_id),
        "last_trade_at": _utc(candle.last_trade_at),
        "last_trade_id": int(candle.last_trade_id),
    }


def _public_delta(trade: Any) -> dict:
    """One public wire delta; no account/debt/intervention fields leak."""
    side = getattr(trade, "side")
    amount = (
        getattr(trade, "input_amount")
        if side == "buy"
        else getattr(trade, "output_amount")
    )
    return {
        "id": int(getattr(trade, "id")),
        "ts": _utc(getattr(trade, "created_at")).isoformat(),
        "post_price": str(_dec(getattr(trade, "post_price"))),
        "gold_volume": str(_dec(amount)),
    }


class FxHistoryRing:
    """Bounded 4-tier OHLCV ring with order-aware merges and Decimal storage.

    Architecture mirrors ``app.services.history_ring.HistoryRing`` (bucket
    windows/segments) but keeps ``Decimal`` values and the ``(created_at, id)``
    first/last ordering keys; no ``float * 1e8`` step, which is unsafe for FX
    rates.  Only the encoder turns values into strings, at the wire boundary.
    """

    def __init__(self) -> None:
        self._tiers: dict[str, dict[int, dict]] = {name: {} for name in RING_SPEC}

    def merge(self, row: dict) -> None:
        interval = row.get("interval")
        tier = RING_SPEC.get(interval)
        if tier is None:
            return
        buckets = self._tiers.setdefault(interval, {})
        epoch = int(_utc(row["bucket_start"]).timestamp())
        existing = buckets.get(epoch)
        buckets[epoch] = dict(row) if existing is None else merge_row(existing, row)

    def prune(self) -> None:
        """Drop buckets that fell out of each tier's window."""
        for interval, buckets in self._tiers.items():
            if not buckets:
                continue
            tier = RING_SPEC[interval]
            newest = max(buckets)
            floor = newest - (tier.buckets - 1) * tier.step
            if min(buckets) < floor:
                for epoch in [e for e in buckets if e < floor]:
                    del buckets[epoch]

    def buckets(self, interval: str) -> dict[int, dict]:
        return self._tiers.get(interval, {})

    def newest_epoch(self, interval: str) -> int | None:
        buckets = self._tiers.get(interval) or {}
        return max(buckets) if buckets else None

    def rows_between(self, interval: str, start_epoch: int, end_epoch: int) -> list[dict]:
        buckets = self._tiers.get(interval) or {}
        return [buckets[e] for e in sorted(e for e in buckets if start_epoch <= e < end_epoch)]


def _encode_segment(buckets: dict[int, dict], interval: str, t0: int, until_exclusive: int) -> dict:
    """Columnar segment with Decimal *strings* (FX-safe codec)."""
    tier = RING_SPEC[interval]
    step = tier.step
    n_buckets = max(0, (until_exclusive - t0 + step - 1) // step) if until_exclusive > t0 else 0
    out: dict[str, Any] = {
        "t0": t0,
        "step": step,
        "n_buckets": n_buckets,
        "t": [],
        "o": [],
        "h": [],
        "l": [],
        "c": [],
        "v": [],
        "trades": [],
    }
    for epoch in sorted(e for e in buckets if t0 <= e < until_exclusive):
        row = buckets[epoch]
        out["t"].append((epoch - t0) // step)
        out["o"].append(str(_dec(row["open_price"])))
        out["h"].append(str(_dec(row["high_price"])))
        out["l"].append(str(_dec(row["low_price"])))
        out["c"].append(str(_dec(row["close_price"])))
        out["v"].append(str(_dec(row["gold_volume"])))
        out["trades"].append(int(row["n_trades"]))
    return out


@dataclass(frozen=True)
class _StateValues:
    pair_id: int
    last_trade_id: int
    history_version: str
    history_ready: bool


@dataclass
class _PairRuntime:
    pair_id: int
    history_version: str
    history_ready: bool
    applied_trade_id: int
    durable_trade_id: int
    queued_trade_id: int
    has_state: bool
    covered_through_at: datetime | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ring: FxHistoryRing = field(default_factory=FxHistoryRing)
    public_trades: deque = field(default_factory=deque)
    public_invalidated: bool = False


SessionFactory = Callable[[], Any]


class FxMarketDataService:
    """Incremental FX candle runtime (process-wide singleton ``FX_MARKET_DATA``)."""

    RECONCILE_INTERVAL = RECONCILE_INTERVAL
    PUBLIC_TRADE_BUFFER = PUBLIC_TRADE_BUFFER

    def __init__(self, factory: SessionFactory = async_session_maker, *, batch_size: int = 1000) -> None:
        if int(batch_size) < 1:
            raise ValueError("batch_size must be positive")
        self._factory = factory
        self._batch_size = int(batch_size)
        self._states: dict[int, _PairRuntime] = {}
        self._flusher = FxCandleFlusher(factory)
        self._dirty: set[int] = set()
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._write_owner = False
        self._started = False

    # ── introspection ─────────────────────────────────────────────────────
    @property
    def write_owner(self) -> bool:
        return self._write_owner

    @property
    def started(self) -> bool:
        return self._started

    @property
    def flusher(self) -> FxCandleFlusher:
        """Owned flusher (read-only handle for sealing checks)."""
        return self._flusher

    def pair_ids(self) -> list[int]:
        return sorted(self._states)

    def state(self, pair_id: int) -> dict | None:
        """Plain per-pair snapshot, or ``None`` when the pair is unknown."""
        rt = self._states.get(int(pair_id))
        if rt is None:
            return None
        return {
            "history_version": rt.history_version,
            "history_ready": rt.history_ready,
            "applied_trade_id": rt.applied_trade_id,
            "durable_trade_id": rt.durable_trade_id,
        }

    # ── post-commit hint ──────────────────────────────────────────────────
    def notify_committed(self, pair_id: int) -> None:
        """Synchronous non-blocking dirty hint; no SQL/ORM objects involved."""
        self._dirty.add(int(pair_id))
        try:
            self._wakeup.set()
        except RuntimeError:  # pragma: no cover - Event has no loop
            pass

    # ── read paths (synchronous, no awaits) ───────────────────────────────
    def get_candles(
        self, pair_id: int, interval: str, start: datetime, end: datetime
    ) -> list[dict] | None:
        """Complete ring rows for ``[start, end)``, else ``None``.

        ``None`` means "the ring cannot answer this window completely" and the
        caller must fall back to persisted candles plus the small unflushed
        projected tail.  Sparse (no-trade) buckets inside the window are
        legitimate and are simply absent.
        """
        rt = self._states.get(int(pair_id))
        if rt is None or not rt.history_ready:
            return None
        tier = RING_SPEC.get(interval)
        if tier is None:
            return None
        start, end = _utc(start), _utc(end)
        if start >= end:
            return None
        through = rt.covered_through_at
        if through is None:
            return None
        step = tier.step
        through_epoch = int(_utc(through).timestamp())
        last_bucket = through_epoch - through_epoch % step
        if int(end.timestamp()) > last_bucket + step:
            return None
        newest = rt.ring.newest_epoch(interval)
        anchor = newest if newest is not None else last_bucket
        floor = anchor - (tier.buckets - 1) * step
        start_epoch = int(start.timestamp())
        if start_epoch < floor:
            return None
        start_bucket = start_epoch - start_epoch % step
        return [
            dict(row)
            for row in rt.ring.rows_between(interval, start_bucket, int(end.timestamp()))
        ]

    def tail(self, pair_id: int, now: datetime) -> dict | None:
        """SSE first-packet tail: version, readiness, per-interval segments."""
        rt = self._states.get(int(pair_id))
        if rt is None:
            return None
        now = _utc(now)
        now_epoch = int(now.timestamp())
        history_tail: dict[str, dict] = {}
        for interval in RING_SPEC:
            tier = RING_SPEC[interval]
            t0 = seal_boundary(interval, now_epoch)
            now_bucket = now_epoch - now_epoch % tier.step
            history_tail[interval] = _encode_segment(
                rt.ring.buckets(interval), interval, t0, now_bucket + tier.step
            )
        return {
            "history_version": rt.history_version,
            "history_ready": rt.history_ready,
            "history_tail": history_tail,
            "history_tail_at": now.isoformat(),
            "history_tail_through_trade_id": rt.applied_trade_id,
        }

    def drain_public_trades(self, pair_id: int) -> tuple[list[dict], bool]:
        """Return pending public deltas and whether the buffer overflowed.

        Discardable publication data only: a ``True`` flag tells the consumer to
        refetch the tail instead of trusting a partial delta list.  It is never
        an input to candle aggregation.
        """
        rt = self._states.get(int(pair_id))
        if rt is None:
            return [], False
        trades = list(rt.public_trades)
        rt.public_trades.clear()
        invalidated = rt.public_invalidated
        rt.public_invalidated = False
        return trades, invalidated

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self, write_owner: bool = False) -> None:
        """Warm persisted state and (owner) compensate every cursor gap.

        The owner path completes all catch-up/flush work before returning, so it
        must run before the economic producers start.  Readers only warm.
        """
        if self._started:
            await self.stop()
        self._states.clear()
        self._dirty.clear()
        self._wakeup = asyncio.Event()
        self._flusher = FxCandleFlusher(self._factory)
        self._write_owner = bool(write_owner)
        self._started = True

        if self._write_owner:
            for pair_id in await self._all_pair_ids():
                if pair_id in self._states:
                    continue
                self._states[pair_id] = await self._load_runtime(
                    pair_id, await self._read_state_values(pair_id)
                )
            for pair_id in sorted(self._states):
                await self.catch_up(pair_id)
            await self.flush_once()
            self._task = asyncio.create_task(self._run(), name="fx-market-state")
        else:
            for values in await self._load_all_state_values():
                self._states[values.pair_id] = await self._load_runtime(
                    values.pair_id, values
                )

    async def stop(self) -> None:
        """Stop the consumer and (owner) catch up + flush remaining pairs."""
        await self._cancel_task()
        if self._write_owner:
            for pair_id in sorted(self._states):
                try:
                    await self.catch_up(pair_id)
                except Exception:  # noqa: BLE001 - shutdown is best effort
                    logger.exception("fx market state final catch-up failed for pair %s", pair_id)
            try:
                await self.flush_once()
            except Exception:  # noqa: BLE001
                logger.exception("fx market state final flush failed")
        self._started = False
        self._write_owner = False

    def reset(self) -> None:
        """Clear local ring/pending/frame metadata (call after tasks stopped)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self._states.clear()
        self._dirty.clear()
        self._flusher = FxCandleFlusher(self._factory)
        self._started = False
        self._write_owner = False

    # ── consumption ───────────────────────────────────────────────────────
    async def catch_up(self, pair_id: int) -> int:
        """Consume committed pair trades and return the in-memory applied cursor.

        Scans the pair's committed high-water mark and pages strictly above the
        applied cursor with a projected column list.  Trade ids need not be
        globally contiguous.  Read-only instances merge into the ring but never
        create a durable state or queue a batch.
        """
        pair_id = int(pair_id)
        rt = self._states.get(pair_id)
        if rt is None:
            if not self._write_owner:
                values = await self._read_state_values(pair_id)
                if values is None:
                    return 0
                rt = await self._load_runtime(pair_id, values)
            else:
                rt = await self._load_runtime(pair_id, await self._read_state_values(pair_id))
            self._states[pair_id] = rt

        async with rt.lock:
            cutoff = await self._pair_max_trade_id(pair_id)
            if cutoff < rt.applied_trade_id:
                cutoff = rt.applied_trade_id
            initial = not rt.history_ready
            last_at: datetime | None = None
            try:
                while rt.applied_trade_id < cutoff:
                    trades = await self._fetch_page(pair_id, rt.applied_trade_id, cutoff)
                    if not trades:
                        break
                    page_through = int(trades[-1].id)
                    rows = compute_fx_candle_rows(trades)
                    ready_after = initial and page_through >= cutoff
                    if self._write_owner:
                        if initial:
                            await self._apply_page(rt, rows, page_through, history_ready=ready_after)
                        else:
                            self._queue_page(rt, rows, page_through)
                    # Merge into the ring only after the durable write was queued:
                    # a rejected/failed page must not be double-merged on retry.
                    for row in rows:
                        rt.ring.merge(row)
                    self._append_public(rt, trades)
                    rt.applied_trade_id = page_through
                    last_at = _utc(trades[-1].created_at)
                    rt.ring.prune()
            except FxCandleCursorConflict:
                logger.error(
                    "fx market state cursor conflict for pair %s; resyncing from durable state",
                    pair_id,
                )
                await self._resync_from_durable(rt)
                raise

            if last_at is not None:
                rt.covered_through_at = last_at
            if self._write_owner and not rt.history_ready and rt.applied_trade_id >= cutoff:
                # Covers both "backfill interrupted right before readiness" and
                # "empty pair first seen with no trades".
                try:
                    await self._ensure_ready(rt)
                except FxCandleCursorConflict:
                    logger.error(
                        "fx market state readiness conflict for pair %s; "
                        "resyncing from durable state",
                        pair_id,
                    )
                    await self._resync_from_durable(rt)
                    raise
            return rt.applied_trade_id

    async def flush_once(self) -> int:
        """Flush owned pending batches and sync local durable metadata."""
        written = await self._flusher.flush_once()
        for pair_id, rt in self._states.items():
            if self._flusher.pending_watermark(pair_id) is None:
                if rt.queued_trade_id > rt.durable_trade_id:
                    rt.durable_trade_id = rt.queued_trade_id
        return written

    # ── background reconciliation ─────────────────────────────────────────
    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=self.RECONCILE_INTERVAL)
                full = False
            except asyncio.TimeoutError:
                full = True
            self._wakeup.clear()
            targets = set(self._dirty)
            self._dirty.clear()
            if full:
                targets.update(self._states)
                try:
                    targets.update(await self._all_pair_ids())
                except Exception:  # noqa: BLE001 - keep the loop alive
                    logger.exception("fx market state pair discovery failed")
            for pair_id in sorted(targets):
                try:
                    await self.catch_up(pair_id)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("fx market state catch-up failed for pair %s", pair_id)
            if self._write_owner:
                try:
                    await self.flush_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("fx market state flush failed")
            if self._dirty:
                self._wakeup.set()

    async def _cancel_task(self) -> None:
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # ── runtime construction ──────────────────────────────────────────────
    async def _load_runtime(self, pair_id: int, values: _StateValues | None) -> _PairRuntime:
        if values is None:
            rt = _PairRuntime(
                pair_id=pair_id,
                history_version=new_history_version(),
                history_ready=False,
                applied_trade_id=0,
                durable_trade_id=0,
                queued_trade_id=0,
                has_state=False,
            )
        else:
            rt = _PairRuntime(
                pair_id=pair_id,
                history_version=values.history_version,
                history_ready=values.history_ready,
                applied_trade_id=values.last_trade_id,
                durable_trade_id=values.last_trade_id,
                queued_trade_id=values.last_trade_id,
                has_state=True,
            )
        await self._warm_ring(rt)
        return rt

    async def _warm_ring(self, rt: _PairRuntime) -> None:
        """Load the pair's retained windows from persisted candles.

        The window anchor is the pair's newest persisted bucket, not wall-clock
        ``now``, so a dormant pair keeps its recent candles in the ring.
        """
        max_window = max(tier.window for tier in RING_SPEC.values())
        async with self._factory() as db:
            latest = (await db.execute(
                select(FxCandle.bucket_start)
                .where(FxCandle.pair_id == rt.pair_id)
                .order_by(FxCandle.bucket_start.desc())
                .limit(1)
            )).scalar()
            if latest is None:
                return
            latest = _utc(latest)
            cutoff = latest - timedelta(seconds=max_window)
            rows = (await db.execute(
                select(FxCandle)
                .where(
                    FxCandle.pair_id == rt.pair_id,
                    FxCandle.interval.in_(list(RING_SPEC.keys())),
                    FxCandle.bucket_start >= cutoff,
                )
                .order_by(FxCandle.bucket_start)
            )).scalars().all()
            for candle in rows:
                tier = RING_SPEC.get(candle.interval)
                if tier is None:
                    continue
                bucket_start = _utc(candle.bucket_start)
                if bucket_start < latest - timedelta(seconds=(tier.buckets - 1) * tier.step):
                    continue
                rt.ring.merge(_candle_to_row(candle, bucket_start))
        rt.ring.prune()
        if rt.covered_through_at is None:
            rt.covered_through_at = latest

    async def _resync_from_durable(self, rt: _PairRuntime) -> None:
        """Rebuild from the persisted checkpoint after a cursor conflict.

        The pending flusher is replaced: its retained ranges may belong to a
        stale history generation (a rebuild changed the state's version) and
        ``add_batch`` refuses to mix generations.  Anything not durable is
        recomputed from ``rt.durable_trade_id`` on the next catch-up, so dropping
        it cannot lose volume.
        """
        values = await self._read_state_values(rt.pair_id)
        self._flusher = FxCandleFlusher(self._factory)
        rt.ring = FxHistoryRing()
        rt.public_trades.clear()
        rt.public_invalidated = False
        rt.covered_through_at = None
        if values is None:
            # The durable row vanished while we were running: never reuse the
            # old generation, cached history under it must not be trusted.
            rt.history_version = new_history_version()
            rt.applied_trade_id = 0
            rt.durable_trade_id = 0
            rt.queued_trade_id = 0
            rt.history_ready = False
            rt.has_state = False
            return
        rt.applied_trade_id = values.last_trade_id
        rt.durable_trade_id = values.last_trade_id
        rt.queued_trade_id = values.last_trade_id
        rt.history_version = values.history_version
        rt.history_ready = values.history_ready
        rt.has_state = True
        await self._warm_ring(rt)

    # ── durable write helpers ─────────────────────────────────────────────
    async def _apply_page(
        self, rt: _PairRuntime, rows: Sequence[dict], through: int, *, history_ready: bool
    ) -> None:
        """Initial-backfill page: atomic candles + cursor (+ readiness)."""
        async with self._factory() as db:
            async with db.begin():
                applied = await apply_candle_batch(
                    db,
                    pair_id=rt.pair_id,
                    expected_trade_id=rt.applied_trade_id,
                    through_trade_id=through,
                    rows=rows,
                    history_version=rt.history_version,
                    history_ready=history_ready,
                )
        rt.has_state = True
        rt.durable_trade_id = max(rt.durable_trade_id, through)
        rt.queued_trade_id = max(rt.queued_trade_id, through)
        if applied and history_ready:
            rt.history_ready = True

    def _queue_page(self, rt: _PairRuntime, rows: Sequence[dict], through: int) -> None:
        """Incremental page for a ready pair: hand to the durable flusher."""
        self._flusher.add_batch(
            rt.pair_id, rt.applied_trade_id, through, rows, rt.history_version
        )
        rt.queued_trade_id = through
        rt.has_state = True

    async def _ensure_ready(self, rt: _PairRuntime) -> None:
        """Explicitly mark a fully-covered pair ready.

        Uses the storage layer's metadata-only batch (``expected == through``,
        no rows, ``history_ready=True``): it creates the state for an empty pair
        and flips readiness on the duplicate path without touching volume.  This
        is the transition the runtime owns; it must never be inferred from a
        volume-bearing batch.
        """
        async with self._factory() as db:
            async with db.begin():
                await apply_candle_batch(
                    db,
                    pair_id=rt.pair_id,
                    expected_trade_id=rt.applied_trade_id,
                    through_trade_id=rt.applied_trade_id,
                    rows=[],
                    history_version=rt.history_version,
                    history_ready=True,
                )
        rt.history_ready = True
        rt.has_state = True
        rt.durable_trade_id = max(rt.durable_trade_id, rt.applied_trade_id)

    def _append_public(self, rt: _PairRuntime, trades: Iterable[Any]) -> None:
        """Buffer bounded, discardable public trade deltas (never candle input)."""
        for trade in trades:
            if len(rt.public_trades) >= self.PUBLIC_TRADE_BUFFER:
                rt.public_trades.clear()
                rt.public_invalidated = True
            if rt.public_invalidated:
                continue
            rt.public_trades.append(_public_delta(trade))

    # ── DB access ─────────────────────────────────────────────────────────
    async def _all_pair_ids(self) -> list[int]:
        async with self._factory() as db:
            return [int(value) for value in (await db.execute(select(FxPair.id))).scalars().all()]

    async def _load_all_state_values(self) -> list[_StateValues]:
        async with self._factory() as db:
            rows = (await db.execute(select(FxMarketDataState))).scalars().all()
            return [
                _StateValues(
                    pair_id=int(row.pair_id),
                    last_trade_id=int(row.last_trade_id),
                    history_version=str(row.history_version),
                    history_ready=bool(row.history_ready),
                )
                for row in rows
            ]

    async def _read_state_values(self, pair_id: int) -> _StateValues | None:
        async with self._factory() as db:
            row = (await db.execute(
                select(FxMarketDataState).where(FxMarketDataState.pair_id == pair_id)
            )).scalars().first()
            if row is None:
                return None
            return _StateValues(
                pair_id=int(row.pair_id),
                last_trade_id=int(row.last_trade_id),
                history_version=str(row.history_version),
                history_ready=bool(row.history_ready),
            )

    async def _pair_max_trade_id(self, pair_id: int) -> int:
        async with self._factory() as db:
            value = (await db.execute(
                select(func.max(FxTrade.id)).where(FxTrade.pair_id == pair_id)
            )).scalar()
        return int(value or 0)

    async def _fetch_page(self, pair_id: int, after: int, through: int) -> list[Any]:
        """Project only the aggregation columns; never load full-day ORM rows."""
        stmt = (
            select(
                FxTrade.id,
                FxTrade.pair_id,
                FxTrade.created_at,
                FxTrade.post_price,
                FxTrade.side,
                FxTrade.input_amount,
                FxTrade.output_amount,
            )
            .where(FxTrade.pair_id == pair_id, FxTrade.id > after, FxTrade.id <= through)
            .order_by(FxTrade.id)
            .limit(self._batch_size)
        )
        async with self._factory() as db:
            return list((await db.execute(stmt)).all())


#: Process-wide singleton used by the commit hooks and the API/lifespan wiring.
FX_MARKET_DATA = FxMarketDataService()
