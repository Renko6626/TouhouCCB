"""Bounded, coalescing post-commit publisher for public FX frames.

The FX trade response path must not wait for the public stream.  Building a
frame reads the committed 24h trade volume, so publication is moved after the
commit into one transient in-process worker fed by a bounded queue:

- ``enqueue`` is synchronous and never blocks, never awaits and never raises;
  a trade therefore cannot fail or slow down because the public stream is
  behind.  A full queue drops the frame and counts it (``stats()['dropped']``).
- Publications are coalesced per pair: only the newest post price matters for
  the wire frame, and the 24h volume is re-read at publish time, so collapsing
  several queued updates into the latest loses no information.
- Exactly one worker task exists between ``start`` and ``stop``; events never
  spawn tasks.  When no SSE subscriber is connected to a pair the frame is
  skipped (counted), because every SSE connection receives a fresh snapshot on
  connect -- a dropped publication can never lose a financial transaction,
  only a best-effort realtime frame.

Lifecycle (owned by the app lifespan, not by this module)::

    await start_publisher()   # after the FX scheduler starts
    ...
    await stop_publisher()    # before the tick broadcaster stops

``drain`` exists for tests and for shutdown so callers can wait until queued
frames have been published (bounded by ``timeout``).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable, Optional

_logger = logging.getLogger(__name__)

DEFAULT_QUEUE_MAXSIZE = 256
DEFAULT_DRAIN_TIMEOUT = 5.0
DEFAULT_STOP_DRAIN_TIMEOUT = 2.0

PublishHook = Callable[["FxPublication"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class FxPublication:
    """One post-commit public frame request.

    Only plain values cross the queue: ORM instances may expire or detach once
    the request transaction is gone, while ``pair_id``/``post_price`` stay
    valid.  ``trade_id`` is kept for diagnostics and is not part of the public
    wire frame.
    """

    pair_id: int
    post_price: Decimal
    trade_id: Optional[int] = None


class FxPublisher:
    """Single-worker bounded queue with per-pair coalescing."""

    def __init__(
        self,
        *,
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        publish: Optional[PublishHook] = None,
        broker: object | None = None,
        stop_drain_timeout: float = DEFAULT_STOP_DRAIN_TIMEOUT,
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be positive")
        self._maxsize = int(maxsize)
        self._publish_hook = publish
        self._broker = broker
        self._stop_drain_timeout = float(stop_drain_timeout)

        self._running = False
        self._task: Optional[asyncio.Task[None]] = None
        self._queue: Optional[asyncio.Queue[int]] = None
        self._idle: Optional[asyncio.Event] = None
        self._pending: dict[int, FxPublication] = {}
        self._queued: set[int] = set()

        self._enqueued = 0
        self._coalesced = 0
        self._dropped = 0
        self._published = 0
        self._skipped = 0
        self._failed = 0

    # ── state ────────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    @property
    def queue_depth(self) -> int:
        return 0 if self._queue is None else self._queue.qsize()

    @property
    def pending_pairs(self) -> int:
        return len(self._pending)

    def stats(self) -> dict[str, int]:
        """Diagnostic counters; safe to call from any coroutine."""
        return {
            "running": int(self.running),
            "queue_depth": self.queue_depth,
            "pending_pairs": self.pending_pairs,
            "enqueued": self._enqueued,
            "coalesced": self._coalesced,
            "dropped": self._dropped,
            "published": self._published,
            "skipped": self._skipped,
            "failed": self._failed,
        }

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Start the worker exactly once; repeated calls are no-ops."""
        if self.running:
            return
        self._queue = asyncio.Queue(maxsize=self._maxsize)
        self._idle = asyncio.Event()
        self._idle.set()
        self._pending = {}
        self._queued = set()
        self._running = True
        self._task = asyncio.create_task(self._run(), name="fx-publisher")

    async def stop(self) -> None:
        """Best-effort flush of queued frames, then cancel the worker."""
        task = self._task
        if task is None:
            self._running = False
            return
        # New enqueues are dropped from here on; already queued frames still
        # get one bounded chance to reach the broker during graceful shutdown.
        self._running = False
        try:
            await self.drain(self._stop_drain_timeout)
        except Exception:  # timeout or a failing publish hook: shutdown must proceed
            _logger.warning("fx publisher did not drain before shutdown", exc_info=True)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._queue = None
        self._idle = None
        self._pending.clear()
        self._queued.clear()

    async def drain(self, timeout: Optional[float] = DEFAULT_DRAIN_TIMEOUT) -> None:
        """Wait until the queue and pending coalesced frames are flushed."""
        idle = self._idle
        if self._task is None or idle is None:
            return
        await asyncio.wait_for(idle.wait(), timeout)

    # ── producer ─────────────────────────────────────────────────────────
    def enqueue(self, pair_id: int, post_price: Decimal, *,
                trade_id: Optional[int] = None) -> bool:
        """Queue one publication; returns False when it was dropped.

        Never blocks and never raises: a stopped worker or a full queue only
        increments the ``dropped`` counter.  The financial transaction that
        produced this frame is already committed by the caller.
        """
        if not self.running or self._queue is None or self._idle is None:
            self._dropped += 1
            return False
        publication = FxPublication(pair_id=pair_id, post_price=Decimal(post_price),
                                    trade_id=trade_id)
        if pair_id in self._queued:
            # A frame for this pair is already queued or in flight: keep only
            # the newest price instead of growing the queue.
            self._pending[pair_id] = publication
            self._coalesced += 1
            return True
        try:
            self._queue.put_nowait(pair_id)
        except asyncio.QueueFull:
            self._dropped += 1
            return False
        self._queued.add(pair_id)
        self._pending[pair_id] = publication
        self._enqueued += 1
        self._idle.clear()
        return True

    # ── worker ───────────────────────────────────────────────────────────
    async def _run(self) -> None:
        queue = self._queue
        assert queue is not None
        while True:
            pair_id = await queue.get()
            try:
                publication = self._pending.pop(pair_id, None)
                if publication is not None:
                    await self._publish(publication)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failed += 1
                _logger.exception("fx public frame publication failed for pair %s", pair_id)
            finally:
                queue.task_done()
            self._queued.discard(pair_id)
            # A newer value may have arrived while this frame was in flight.
            if pair_id in self._pending:
                try:
                    queue.put_nowait(pair_id)
                    self._queued.add(pair_id)
                except asyncio.QueueFull:
                    self._pending.pop(pair_id, None)
                    self._dropped += 1
            if queue.empty() and not self._pending:
                if self._idle is not None:
                    self._idle.set()

    async def _publish(self, publication: FxPublication) -> None:
        if self._publish_hook is not None:
            await self._publish_hook(publication)
            self._published += 1
            return
        # Imported lazily so tests can monkeypatch the module functions and so
        # this module never participates in an import cycle with trading.
        from app.services.fx import market_data
        from app.services.realtime import BROKER

        broker = self._broker if self._broker is not None else BROKER
        if broker.subscriber_count(publication.pair_id) == 0:
            # Every SSE connection reads a fresh snapshot on connect, so an
            # unwatched pair needs no frame and no 24h volume query.
            self._skipped += 1
            return
        await market_data.publish_pair_frame(
            publication.pair_id, publication.post_price, broker=broker)
        self._published += 1


# Process-wide singleton fed by the request path and driven by the lifespan.
PUBLISHER = FxPublisher()


def enqueue_publication(pair_id: int, post_price: Decimal, *,
                        trade_id: Optional[int] = None) -> bool:
    """Non-blocking best-effort publication; see :meth:`FxPublisher.enqueue`."""
    return PUBLISHER.enqueue(pair_id, post_price, trade_id=trade_id)


async def start_publisher() -> None:
    """Lifespan hook: start the single publisher worker (idempotent)."""
    await PUBLISHER.start()


async def stop_publisher() -> None:
    """Lifespan hook: bounded drain and cancel of the publisher worker."""
    await PUBLISHER.stop()


async def drain_publisher(timeout: Optional[float] = DEFAULT_DRAIN_TIMEOUT) -> None:
    """Test/shutdown helper: wait for queued frames up to ``timeout`` seconds."""
    await PUBLISHER.drain(timeout)


def publisher_stats() -> dict[str, int]:
    """Diagnostic counters of the process-wide publisher."""
    return PUBLISHER.stats()
