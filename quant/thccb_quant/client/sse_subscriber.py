"""SseSubscriber: 协调多 market SSE 订阅 + 写 trades 表 + dispatch
on_sse_event。spec §6。
"""
import asyncio
from typing import Iterable

import structlog

from thccb_quant.client.rest import RestClient
from thccb_quant.client.sse import SseClient, SseEvent
from thccb_quant.state.store import Store
from thccb_quant.strategy.base import Strategy


class SseSubscriber:
    def __init__(
        self,
        *,
        rest: RestClient,
        store: Store,
        sse_client: SseClient,
        strategies: list[Strategy],
        market_ids: Iterable[int],
        logger: structlog.BoundLogger,
    ):
        self._rest = rest
        self._store = store
        self._sse = sse_client
        self._strategies = list(strategies)
        self._market_ids = set(market_ids)
        self._log = logger

    async def run(self) -> None:
        await self._preload_partial_trades()
        if not self._market_ids:
            self._log.info("sse_no_markets_to_subscribe")
            return
        tasks = [
            asyncio.create_task(self._watch_market(mid))
            for mid in self._market_ids
        ]
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _preload_partial_trades(self) -> None:
        try:
            trades = await self._rest.get_recent_trades(limit=100)
            items = [t.model_dump() for t in trades]
            inserted = await self._store.bulk_insert_partial_trades(items)
            self._log.info("sse_partial_trades_preloaded",
                           returned=len(items), inserted=inserted)
        except Exception:
            self._log.exception("sse_partial_trades_preload_failed_continuing")

    async def _watch_market(self, market_id: int) -> None:
        async for event in self._sse.subscribe(market_id):
            try:
                await self._handle_event(market_id, event)
            except Exception:
                self._log.exception("sse_handle_event_failed",
                                    market_id=market_id, seq=event.seq)

    async def _handle_event(self, market_id: int, event: SseEvent) -> None:
        if event.type == "trade":
            await self._store.log_trade(market_id=market_id, payload=event.data)
            await self._dispatch(market_id, event)
        elif event.type == "market_status":
            self._log.info("sse_market_status",
                           market_id=market_id,
                           status=event.data.get("status"))
            await self._dispatch(market_id, event)
        elif event.type == "snapshot":
            self._log.info("sse_snapshot_bootstrapped",
                           market_id=market_id, seq=event.seq,
                           gap_recover=event.data.get("gap_recover", False))

    async def _dispatch(self, market_id: int, event: SseEvent) -> None:
        for s in self._strategies:
            if getattr(s, "market_id", None) != market_id:
                continue
            try:
                await s.on_sse_event(event)
            except Exception:
                self._log.exception("sse_on_sse_event_failed",
                                    strategy=s.name, market_id=market_id)
