"""One bounded command consumer per FX pair; money remains database-authoritative."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select

from app.core import database
from app.models.fx import FxPair
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import OWNERSHIP
from app.services.market_read_state import FxReadState

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FxWriterState:
    snapshot: FxReadState
    unavailable: bool = False
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class ReloadPair:
    pair_id: int


class FxWriter:
    QUEUE_MAXSIZE = 256
    SUBMIT_TIMEOUT = 10.0

    def __init__(self):
        self._states: dict[int, FxWriterState] = {}
        self._queues: dict[int, asyncio.Queue] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._ops = {}
        self._enabled = False
        self._bootstrap_lock = asyncio.Lock()

    @property
    def enabled(self):
        return self._enabled

    def get_state(self, pair_id):
        return self._states.get(pair_id)

    def register_op(self, command_type, op):
        """An op owns its fresh session/transaction and installs before returning."""
        self._ops[command_type] = op

    async def start(self):
        OWNERSHIP.require_writes()
        await self.stop()
        from app.services.fx.writer_ops import register_all_ops
        register_all_ops(self)
        async with database.async_session_maker() as db:
            ids = list((await db.execute(select(FxPair.id))).scalars())
        try:
            for pair_id in ids:
                await self._bootstrap(pair_id)
        except BaseException:
            await self.stop()
            raise
        self._enabled = True

    async def stop(self):
        self._enabled = False
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for queue in self._queues.values():
            while not queue.empty():
                _, future = queue.get_nowait()
                if not future.done():
                    future.set_exception(HTTPException(503, "FX writer is stopping"))
        for pair_id in self._states:
            self._invalidate(pair_id)
        self._tasks.clear()
        self._queues.clear()
        self._states.clear()

    @staticmethod
    def _invalidate(pair_id):
        from app.schemas.trading_context import GroupKey as PricingKey
        from app.services.trading_prices import REGISTRY
        REGISTRY.invalidate(PricingKey(product="fx", id=pair_id))

    def install_committed(self, snapshot: FxReadState):
        """Synchronous publication; installation failure makes the actor unavailable."""
        from app.services.trading_prices import REGISTRY
        try:
            REGISTRY.install_committed(snapshot)
        except BaseException:
            self._invalidate(snapshot.id)
            state = self._states.get(snapshot.id)
            if state is not None:
                state.unavailable = True
            raise
        state = self._states.get(snapshot.id)
        if state is not None:
            state.snapshot = snapshot
            state.unavailable = False

    def mark_deleted(self, pair_id):
        """Consumer-only deletion: retire this ID and complete every queued waiter."""
        self._states[pair_id].unavailable = True
        self._states[pair_id].deleted = True
        self._invalidate(pair_id)
        queue = self._queues[pair_id]
        while not queue.empty():
            _, future = queue.get_nowait()
            if not future.done():
                future.set_exception(HTTPException(404, "FX pair was deleted"))

    async def _load_direct(self, pair_id):
        """Bootstrap/recovery only: never enqueue from this consumer into itself."""
        async with GATES.hold(exclusive=[GroupKey("fx", pair_id)]):
            try:
                async with database.async_session_maker() as db:
                    pair = await db.get(FxPair, pair_id)
                    if pair is None:
                        raise HTTPException(404, "FX pair not found")
                    snapshot = FxReadState.from_pair(pair)
                self.install_committed(snapshot)
                return snapshot
            except BaseException:
                state = self._states.get(pair_id)
                if state is not None:
                    state.unavailable = True
                self._invalidate(pair_id)
                raise

    async def _bootstrap(self, pair_id):
        snapshot = await self._load_direct(pair_id)
        self._states[pair_id] = FxWriterState(snapshot)
        self._queues[pair_id] = asyncio.Queue(maxsize=self.QUEUE_MAXSIZE)
        self._tasks[pair_id] = asyncio.create_task(
            self._consume(pair_id), name=f"fx-writer-{pair_id}")

    async def register_pair(self, pair_id):
        """After creation commits and closes, load before exposing its queue."""
        OWNERSHIP.require_writes()
        if not self._enabled:
            raise HTTPException(503, "FX writer is unavailable")
        if GATES.held_keys_by_current_task():
            raise RuntimeError("Release all symbol gates before registering an FX pair")
        async with self._bootstrap_lock:
            if pair_id not in self._queues:
                await self._bootstrap(pair_id)
                return
        await self.reload_state(pair_id)

    async def reload_state(self, pair_id):
        return await self.submit(ReloadPair(pair_id))

    async def submit(self, command):
        OWNERSHIP.require_writes()
        if GATES.held_keys_by_current_task():
            raise RuntimeError("Release all symbol gates before awaiting an FX writer command")
        pair_id = command.pair_id
        queue = self._queues.get(pair_id)
        state = self._states.get(pair_id)
        if not self._enabled:
            raise HTTPException(503, "FX writer is unavailable")
        if queue is None or state is None:
            raise HTTPException(404, "FX pair is not registered")
        if state.deleted:
            raise HTTPException(404, "FX pair was deleted")
        if state.unavailable and not isinstance(command, ReloadPair):
            raise HTTPException(503, "FX pair state is unavailable; retry later")
        future = asyncio.get_running_loop().create_future()
        try:
            queue.put_nowait((command, future))
        except asyncio.QueueFull:
            raise HTTPException(429, "FX trading is busy; retry later") from None
        try:
            return await asyncio.wait_for(future, timeout=self.SUBMIT_TIMEOUT)
        except asyncio.TimeoutError:
            raise HTTPException(503, "FX operation result unknown; refresh to confirm") from None

    async def _consume(self, pair_id):
        queue = self._queues[pair_id]
        while True:
            command, future = await queue.get()
            if future.done():
                continue  # A request cancelled before execution must not trade later.
            try:
                OWNERSHIP.require_writes()
                if isinstance(command, ReloadPair):
                    response = await self._load_direct(pair_id)
                else:
                    if self._states[pair_id].unavailable:
                        raise HTTPException(503, "FX pair state is unavailable")
                    response = await self._ops[type(command)](self, command)
                if not future.done():
                    future.set_result(response)
            except asyncio.CancelledError:
                self._invalidate(pair_id)
                if not future.done():
                    future.set_exception(HTTPException(503, "FX writer is stopping; result unknown"))
                raise
            except Exception as exc:
                # Expected business rejections preserve their public exception/DTO.
                # Unexpected failures can follow a durable commit: reload before
                # executing the next command, and reject while recovery is broken.
                from app.services.admin_user_service import AdminUserError
                from app.services.fx.trading import TradeRejected
                from app.services.fx.shorts import ShortRejected
                if not isinstance(exc, (HTTPException, TradeRejected, ShortRejected, AdminUserError)):
                    self._states[pair_id].unavailable = True
                    self._invalidate(pair_id)
                    logger.exception("FX writer command failed: pair=%s", pair_id)
                    try:
                        await self._load_direct(pair_id)
                    except Exception:
                        logger.exception("FX writer recovery failed: pair=%s", pair_id)
                if not future.done():
                    future.set_exception(exc)


FX_WRITER = FxWriter()
