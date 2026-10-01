"""Durable FX candle batch flusher (storage half of the Task 2 interface).

The runtime service (``market_state.py``, implemented separately) computes rows
and calls :meth:`FxCandleFlusher.add_batch` with the in-memory expected cursor.
:meth:`flush_once` writes each pair's queued ranges in order, one transaction
per range, via :func:`~app.services.fx.candles.apply_candle_batch`, which owns
the atomic cursor/generation check.

No-loss queueing
----------------
``flush_once`` **detaches and freezes** the head range (expected, through,
version, readiness and a row snapshot) before awaiting the database, records it
as *in-flight*, and only removes it by identity once its transaction resolves.
A concurrent :meth:`add_batch` therefore can never extend a range that is being
committed, and a newly queued contiguous range is preserved for the next step
instead of being deleted with the processed object.  :meth:`flush_once` calls
serialize on an internal lock, so a 5 s tick and a shutdown flush cannot process
the same range twice.

Uncertain-commit safety
-----------------------
A failed flush is treated as *commit outcome unknown*: the frozen range is
re-inserted at the front, marked ``uncertain``, and later ranges wait.  The next
flush retries it against the durable cursor (already durable → dropped without
re-adding volume; not durable → applied).  Disjoint ranges are never merged
across an uncertain or in-flight boundary, so the queue can never claim an
``expected`` that includes a possibly-committed prefix.

The runtime owns ``start``/``stop`` lifecycle and may call :meth:`discard_pair`
to drop one pair's pending generation without disturbing other pairs.  Candles
are never coupled to the discardable realtime publisher queue.
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
    # True once discard_pair() abandoned this range/generation.
    discarded: bool = False
    rows: dict[RowKey, dict] = field(default_factory=dict)


class FxCandleFlusher:
    """Ordered per-pair pending candle ranges with order-aware in-memory merge."""

    FLUSH_INTERVAL = 5.0

    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory
        self._pending: dict[int, list[_PendingBatch]] = {}
        self._in_flight: dict[int, _PendingBatch] = {}
        self._flush_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # ── pending state ─────────────────────────────────────────────────────
    def pending_count(self) -> int:
        """Pairs with queued or in-flight ranges."""
        return len(set(self._pending) | set(self._in_flight))

    def pending_watermark(self, pair_id: int) -> int | None:
        """Lowest durable cursor not yet covered by a pending/in-flight range.

        Trades with ``id <= watermark`` are already in the database.  ``None``
        means the pair has no queued work (read ``FxMarketDataState`` for the
        durable cursor).  While a range is in-flight its (frozen) ``expected`` is
        reported, because its commit outcome is still unknown.
        """
        in_flight = self._in_flight.get(pair_id)
        if in_flight is not None:
            return in_flight.expected_trade_id
        batches = self._pending.get(pair_id)
        return None if not batches else batches[0].expected_trade_id

    def pending_through(self, pair_id: int) -> int | None:
        """Highest trade id queued or in-flight for ``pair_id``."""
        candidates = []
        in_flight = self._in_flight.get(pair_id)
        if in_flight is not None:
            candidates.append(in_flight.through_trade_id)
        batches = self._pending.get(pair_id)
        if batches:
            candidates.append(batches[-1].through_trade_id)
        return max(candidates) if candidates else None

    def pending_ranges(self, pair_id: int) -> int:
        """Queued ranges for ``pair_id``, counting an in-flight range."""
        return len(self._pending.get(pair_id, ())) + (1 if pair_id in self._in_flight else 0)

    def oldest_pending_bucket(self, pair_id: int | None = None) -> datetime | None:
        """Earliest bucket start among pending/in-flight rows (sealing high-water)."""
        buckets = [
            row["bucket_start"]
            for pid, batches in self._pending.items()
            if pair_id is None or pid == pair_id
            for batch in batches
            for row in batch.rows.values()
        ]
        in_flight = self._in_flight.values() if pair_id is None else (
            [self._in_flight[pair_id]] if pair_id in self._in_flight else []
        )
        for batch in in_flight:
            buckets.extend(row["bucket_start"] for row in batch.rows.values())
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
        initial/rebuild call passes ``True``).  Rules:

        * a non-empty ``expected == through`` batch is rejected immediately
          (never queued into a permanent failure);
        * same ``expected`` narrower ``through`` is ignored — the wider queued
          range stays authoritative and its rows are never replaced by a subset;
        * same ``expected`` equal ``through`` replaces rows; a wider ``through``
          replaces and widens only when the range has never been flush-attempted
          (never across an uncertain or in-flight prefix);
        * ``expected == tail through`` merges only into a never-attempted tail,
          otherwise it becomes a separate retained range;
        * a covered range is ignored; a genuine gap or mixed generation raises
          :class:`~app.services.fx.candles.FxCandleCursorConflict`.
        """
        if through_trade_id < expected_trade_id:
            raise ValueError(
                f"through_trade_id {through_trade_id} < expected_trade_id {expected_trade_id}"
            )
        if through_trade_id == expected_trade_id and rows:
            raise ValueError(
                f"pair {pair_id} batch ({expected_trade_id}, {through_trade_id}] "
                "consumes no trade but carries candle rows"
            )

        batches = self._pending.get(pair_id)
        in_flight = self._in_flight.get(pair_id)

        if not batches:
            self._append_or_create(pair_id, expected_trade_id, through_trade_id, rows,
                                   history_version, history_ready, in_flight)
            return

        if any(batch.history_version != history_version for batch in batches):
            raise FxCandleCursorConflict(
                f"pair {pair_id} has pending ranges for a different history "
                f"generation; discard or flush them before generation {history_version!r}"
            )

        match = next(
            (batch for batch in batches if batch.expected_trade_id == expected_trade_id),
            None,
        )
        if match is not None:
            self._merge_match(match, through_trade_id, rows, history_ready)
            return

        tail = batches[-1]
        if expected_trade_id == tail.through_trade_id and through_trade_id >= expected_trade_id:
            if tail.uncertain:
                batches.append(self._new_batch(
                    pair_id, expected_trade_id, through_trade_id, rows,
                    history_version, history_ready,
                ))
            else:
                tail.through_trade_id = through_trade_id
                if history_ready is not None:
                    tail.history_ready = history_ready
                for row in rows:
                    key = _row_key(row)
                    existing = tail.rows.get(key)
                    tail.rows[key] = merge_row(existing, row) if existing else dict(row)
            return

        if through_trade_id <= tail.through_trade_id and expected_trade_id >= tail.expected_trade_id:
            return  # already covered by the tail
        raise FxCandleCursorConflict(
            f"pair {pair_id} queued ranges cover through {tail.through_trade_id}, "
            f"cannot add ({expected_trade_id}, {through_trade_id}]"
        )

    def _append_or_create(self, pair_id, expected, through, rows, version, ready, in_flight):
        """No queued range: create, or attach after an in-flight head."""
        if in_flight is None:
            self._pending[pair_id] = [self._new_batch(pair_id, expected, through, rows, version, ready)]
            return
        if through <= in_flight.through_trade_id or expected == in_flight.expected_trade_id:
            # Covered/stale, or an overlapping recompute of the frozen in-flight
            # range that must not be mutated while its commit is unknown.
            return
        if expected == in_flight.through_trade_id:
            self._pending[pair_id] = [self._new_batch(pair_id, expected, through, rows, version, ready)]
            return
        raise FxCandleCursorConflict(
            f"pair {pair_id} in-flight range ends at {in_flight.through_trade_id}, "
            f"cannot add ({expected}, {through}]"
        )

    def _merge_match(self, match, through, rows, history_ready):
        if through < match.through_trade_id:
            return  # narrower recompute must not replace the wider authoritative rows
        if through == match.through_trade_id:
            match.rows = {_row_key(row): dict(row) for row in rows}
            if history_ready is not None:
                match.history_ready = history_ready
            return
        if match.uncertain:
            return  # never widen a possibly-committed prefix
        match.rows = {_row_key(row): dict(row) for row in rows}
        match.through_trade_id = through
        if history_ready is not None:
            match.history_ready = history_ready

    @staticmethod
    def _new_batch(pair_id, expected, through, rows, version, ready) -> _PendingBatch:
        return _PendingBatch(
            pair_id=pair_id,
            expected_trade_id=expected,
            through_trade_id=through,
            history_version=version,
            history_ready=ready,
            rows={_row_key(row): dict(row) for row in rows},
        )

    # ── per-pair reset ────────────────────────────────────────────────────
    def discard_pair(self, pair_id: int) -> None:
        """Drop ALL pending work for ``pair_id``; other pairs are untouched.

        An in-flight range for the pair is marked discarded so a failure does
        not re-insert it: a stale-generation retry cannot resurrect it.  This is
        a queue reset only; it does not cancel an already-open DB transaction,
        so the runtime must call it inside its per-pair maintenance boundary and
        rotate the generation after the in-flight attempt has settled.
        """
        self._pending.pop(pair_id, None)
        in_flight = self._in_flight.get(pair_id)
        if in_flight is not None:
            in_flight.discarded = True

    # ── persistence ───────────────────────────────────────────────────────
    async def flush_once(self) -> int:
        """Write queued ranges in order; return newly upserted candle rows.

        Serialized against other ``flush_once`` calls.  Each head range is
        detached and frozen before awaiting, so ranges added meanwhile survive.
        A failed range is re-inserted at the front and marked uncertain, and
        later ranges for that pair wait.  An already-durable range contributes
        no rows.
        """
        async with self._flush_lock:
            return await self._flush_locked()

    async def _flush_locked(self) -> int:
        if not self._pending:
            return 0
        written = 0
        for pair_id in list(self._pending.keys()):
            # Only drain ranges queued at entry: a range added while we await is
            # preserved for the next flush instead of extending this call.
            batches = self._pending.get(pair_id)
            remaining = len(batches) if batches else 0
            while remaining > 0:
                batches = self._pending.get(pair_id)
                if not batches:
                    self._pending.pop(pair_id, None)
                    break
                batch = batches.pop(0)
                remaining -= 1
                if not batches:
                    self._pending.pop(pair_id, None)

                # Freeze everything the commit depends on: add_batch may run
                # while we await, and must not affect this transaction.
                expected = batch.expected_trade_id
                through = batch.through_trade_id
                history_version = batch.history_version
                history_ready = batch.history_ready
                rows = list(batch.rows.values())
                self._in_flight[pair_id] = batch
                try:
                    try:
                        async with self._factory() as db:
                            async with db.begin():
                                applied = await apply_candle_batch(
                                    db,
                                    pair_id=pair_id,
                                    expected_trade_id=expected,
                                    through_trade_id=through,
                                    rows=rows,
                                    history_version=history_version,
                                    history_ready=history_ready,
                                )
                    except FxCandleCursorConflict:
                        logger.exception(
                            "fx candle cursor conflict for pair %s; retaining ranges", pair_id
                        )
                        self._fail_batch(pair_id, batch)
                        break
                    except Exception:
                        logger.exception(
                            "fx candle flush failed for pair %s; retaining ranges", pair_id
                        )
                        self._fail_batch(pair_id, batch)
                        break
                    except asyncio.CancelledError:
                        self._fail_batch(pair_id, batch)
                        raise
                finally:
                    self._in_flight.pop(pair_id, None)

                if applied:
                    written += len(rows)
        return written

    def _fail_batch(self, pair_id: int, batch: _PendingBatch) -> None:
        if batch.discarded:
            return
        batch.uncertain = True
        self._pending.setdefault(pair_id, []).insert(0, batch)

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
