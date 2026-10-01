"""Durable FX candle batch flusher (storage half of the Task 2 interface).

The runtime service (``market_state.py``, implemented separately) computes rows
and calls :meth:`FxCandleFlusher.add_batch` with the in-memory expected cursor.
:meth:`flush_once` writes each pair's queued ranges in order, one transaction
per range, via :func:`~app.services.fx.candles.apply_candle_batch`, which owns
the atomic cursor/generation check.

Uncertain-commit safety
-----------------------
A failed flush is treated as *commit outcome unknown*: the range is retained and
marked ``uncertain``, and the next flush retries it against the durable cursor
(already durable → dropped without re-adding volume; not durable → applied).

Ranges are kept **separate** across an uncertain boundary.  A new disjoint range
(``expected == queued through``) is appended instead of merged when the queued
tail is uncertain, so the queue can never claim an ``expected`` that includes a
possibly-committed prefix.  Merging only happens while the tail has never been
flush-attempted.  A recompute that overlaps an uncertain range is ignored (the
periodic catch-up rebuilds it from the durable cursor once the head resolves)
and reclaming an uncertain range with a wider ``through`` is refused rather than
widened.  If a queued range fails, later ranges wait; they are never applied
ahead of it.

The runtime owns ``start``/``stop`` lifecycle.  Candles are never coupled to the
discardable realtime publisher queue.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Sequence

from app.services.fx.candles import FxCandleCursorConflict, apply_candle_batch, merge_row

logger = logging.getLogger(__name__)

# Async session factory: callable returning an async context manager session.
SessionFactory = Callable[[], Any]

RowKey = tuple[int, str, datetime]


def _row_key(row: dict) -> RowKey:
    return (int(row["pair_id"]), row["interval"], row["bucket_start"])


@dataclass(eq=False)
class _PendingBatch:
    pair_id: int
    expected_trade_id: int
    through_trade_id: int
    history_version: str
    history_ready: bool | None = None
    # True once a flush attempt failed and the commit outcome is unknown.
    uncertain: bool = False
    rows: dict[RowKey, dict] = field(default_factory=dict)


class FxCandleFlusher:
    """Ordered per-pair pending candle ranges with order-aware in-memory merge."""

    FLUSH_INTERVAL = 5.0

    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory
        self._pending: dict[int, list[_PendingBatch]] = {}
        self._task: asyncio.Task | None = None

    # ── pending state ─────────────────────────────────────────────────────
    def pending_count(self) -> int:
        return len(self._pending)

    def pending_watermark(self, pair_id: int) -> int | None:
        """Lowest durable cursor not yet covered by a pending range for ``pair_id``.

        Trades with ``id <= watermark`` are already in the database.  ``None``
        means the pair has no pending range (read ``FxMarketDataState`` for the
        durable cursor).
        """
        batches = self._pending.get(pair_id)
        return None if not batches else batches[0].expected_trade_id

    def pending_through(self, pair_id: int) -> int | None:
        """Highest trade id queued for ``pair_id`` but not yet flushed."""
        batches = self._pending.get(pair_id)
        return None if not batches else batches[-1].through_trade_id

    def pending_ranges(self, pair_id: int) -> int:
        """Number of queued ranges for ``pair_id`` (>1 only across uncertainty)."""
        return len(self._pending.get(pair_id, ()))

    def oldest_pending_bucket(self, pair_id: int | None = None) -> datetime | None:
        """Earliest bucket start among pending rows (sealing high-water)."""
        buckets = [
            row["bucket_start"]
            for pid, batches in self._pending.items()
            if pair_id is None or pid == pair_id
            for batch in batches
            for row in batch.rows.values()
        ]
        return min(buckets) if buckets else None

    # ── queueing ──────────────────────────────────────────────────────────
    def add_batch(
        self,
        pair_id: int,
        expected_trade_id: int,
        through_trade_id: int,
        rows: Sequence[dict],
        history_version: str,
        history_ready: bool | None = None,
    ) -> None:
        """Queue ``(expected_trade_id, through_trade_id]`` rows for one pair.

        ``history_ready=None`` preserves readiness (``False`` until an explicit
        initial/rebuild call passes ``True``).  Overlap policy:

        * same ``expected`` on a never-attempted range → authoritative replace
          (may widen ``through``); on an uncertain range → replace only for the
          same ``through``, a wider recompute is refused;
        * ``expected == queued through`` → contiguous merge while the tail has
          never been attempted, otherwise a separate retained range;
        * an already-covered range is ignored; a genuine gap or a stale
          generation raises :class:`~app.services.fx.candles.FxCandleCursorConflict`.
        """
        if through_trade_id < expected_trade_id:
            raise ValueError(
                f"through_trade_id {through_trade_id} < expected_trade_id {expected_trade_id}"
            )
        batches = self._pending.get(pair_id)
        if not batches:
            self._pending[pair_id] = [
                _PendingBatch(
                    pair_id=pair_id,
                    expected_trade_id=expected_trade_id,
                    through_trade_id=through_trade_id,
                    history_version=history_version,
                    history_ready=history_ready,
                    rows={_row_key(row): dict(row) for row in rows},
                )
            ]
            return

        if any(batch.history_version != history_version for batch in batches):
            raise FxCandleCursorConflict(
                f"pair {pair_id} has pending ranges for a different history "
                f"generation; flush or reset them before generation {history_version!r}"
            )

        match = next(
            (batch for batch in batches if batch.expected_trade_id == expected_trade_id),
            None,
        )
        if match is not None:
            if match.uncertain:
                if through_trade_id != match.through_trade_id:
                    # The prefix may already be durable; never widen it.
                    return
                match.rows = {_row_key(row): dict(row) for row in rows}
            else:
                match.rows = {_row_key(row): dict(row) for row in rows}
                if through_trade_id > match.through_trade_id:
                    match.through_trade_id = through_trade_id
            if history_ready is not None:
                match.history_ready = history_ready
            return

        tail = batches[-1]
        if expected_trade_id == tail.through_trade_id and through_trade_id >= tail.through_trade_id:
            if tail.uncertain:
                batches.append(
                    _PendingBatch(
                        pair_id=pair_id,
                        expected_trade_id=expected_trade_id,
                        through_trade_id=through_trade_id,
                        history_version=history_version,
                        history_ready=history_ready,
                        rows={_row_key(row): dict(row) for row in rows},
                    )
                )
                return
            tail.through_trade_id = through_trade_id
            if history_ready is not None:
                tail.history_ready = history_ready
            for row in rows:
                key = _row_key(row)
                existing = tail.rows.get(key)
                tail.rows[key] = merge_row(existing, row) if existing else dict(row)
            return

        if through_trade_id <= tail.through_trade_id:
            # Already covered by the queued (or a previously flushed) range.
            return
        raise FxCandleCursorConflict(
            f"pair {pair_id} queued ranges cover through {tail.through_trade_id}, "
            f"cannot add ({expected_trade_id}, {through_trade_id}]"
        )

    # ── persistence ───────────────────────────────────────────────────────
    async def flush_once(self) -> int:
        """Write every queued range in order; return newly written candle rows.

        A failed range is retained and marked uncertain, and later ranges for
        that pair wait.  An already-durable range is dropped without re-adding
        volume.
        """
        if not self._pending:
            return 0
        written = 0
        for pair_id, batches in list(self._pending.items()):
            for batch in list(batches):
                rows = list(batch.rows.values())
                try:
                    async with self._factory() as db:
                        async with db.begin():
                            applied = await apply_candle_batch(
                                db,
                                pair_id=pair_id,
                                expected_trade_id=batch.expected_trade_id,
                                through_trade_id=batch.through_trade_id,
                                rows=rows,
                                history_version=batch.history_version,
                                history_ready=batch.history_ready,
                            )
                except FxCandleCursorConflict:
                    logger.exception(
                        "fx candle cursor conflict for pair %s; retaining ranges", pair_id
                    )
                    batch.uncertain = True
                    break
                except Exception:
                    logger.exception(
                        "fx candle flush failed for pair %s; retaining ranges", pair_id
                    )
                    batch.uncertain = True
                    break
                if applied:
                    written += len(rows)
                if batch in batches:
                    batches.remove(batch)
            if not batches:
                self._pending.pop(pair_id, None)
        return written

    # ── lifecycle (owned by the runtime) ──────────────────────────────────
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="fx-candle-flusher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush_once()  # graceful shutdown must not drop pending rows

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.FLUSH_INTERVAL)
            try:
                await self.flush_once()
            except Exception:
                logger.exception("fx candle flusher loop error")
