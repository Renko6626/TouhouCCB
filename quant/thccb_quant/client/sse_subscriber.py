"""SseSubscriber: 协调多 market SSE 订阅 + 写 trades 表 + dispatch
on_sse_event。spec §6。
"""
import asyncio
import time
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
        dispatch_timeout_sec: float = 15.0,
    ):
        # dispatch_timeout_sec 默认 15s：基于 RestClient 最多重试 3 次 × 10s
        # + backoff ≈ 14s 单 broker 调用链最坏情况。
        self._rest = rest
        self._store = store
        self._sse = sse_client
        self._strategies = list(strategies)
        self._market_ids = set(market_ids)
        self._log = logger
        self._dispatch_timeout_sec = dispatch_timeout_sec
        self._last_event_handled_ts = time.monotonic()
        # 诊断计数：trade event 因 trade_id 重复被跳过的总次数。
        # 突然涨 = backend SSE race 或 重连风暴异常；正常 << 1/分钟。
        self._dedup_skipped_count = 0
        self._last_status: dict[int, str] = {}
        self._tick_dedup_count = 0

    @property
    def last_event_handled_ts(self) -> float:
        """monotonic 时间戳，每次 _handle_event 完成（无论 dispatch 结果）后更新。"""
        return self._last_event_handled_ts

    @property
    def dedup_skipped_count(self) -> int:
        """累计 dedup 触发次数；WebUI 用以监控 SSE 流是否健康。"""
        return self._dedup_skipped_count

    @property
    def tick_dedup_count(self) -> int:
        """帧内成交与 legacy 事件重复被跳过的次数（双发期设计内常态，与
        dedup_skipped_count 的异常告警语义分开）。"""
        return self._tick_dedup_count

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
        try:
            if event.type == "trade":
                # log_trade 返回 False 表示这个 trade_id 已经在 DB 里，是重复事件
                # （backend snapshot+event 双推 / SSE 重连后重发 / etc.）。
                # 跳过 dispatch 防策略 EMA 等内部状态多更新一次。
                is_new = await self._store.log_trade(
                    market_id=market_id, payload=event.data,
                )
                if not is_new:
                    trade_id = None
                    try:
                        trade_id = int(event.data["trade"]["id"])
                    except (KeyError, TypeError, ValueError):
                        pass
                    self._dedup_skipped_count += 1
                    self._log.info(
                        "sse_trade_dedup_skipped",
                        market_id=market_id, seq=event.seq, trade_id=trade_id,
                        total_dedup_skipped=self._dedup_skipped_count,
                    )
                    return
                await self._dispatch(market_id, event)
            elif event.type == "market_status":
                self._log.info("sse_market_status",
                               market_id=market_id,
                               status=event.data.get("status"))
                await self._dispatch(market_id, event)
            elif event.type == "tick":
                await self._handle_tick(market_id, event)
            elif event.type == "snapshot":
                self._log.info("sse_snapshot_bootstrapped",
                               market_id=market_id, seq=event.seq,
                               gap_recover=event.data.get("gap_recover", False))
                self._last_status[market_id] = str(event.data.get("status", ""))
            elif event.type == "ping":
                # ping 不 dispatch（策略不需要）；进 finally 块刷 _last_event_handled_ts
                # 让冷市场 25s 一次的 ping 也算 alive proof，避免被 WebUI 误标 stalled
                # 或 liveness_watchdog 误杀。
                pass
        finally:
            # liveness watchdog 依赖该 ts：无论 event 类型 / dispatch 成败都得推进
            self._last_event_handled_ts = time.monotonic()

    async def _handle_tick(self, market_id: int, event: SseEvent) -> None:
        """tick 帧适配器（主站阶段 2）：帧 → 合成逐笔 trade / market_status 事件，
        喂给既有 dispatch，策略层零改动。双发期与 legacy 事件的重复靠
        log_trade 的 trade_id 去重，静默跳过。"""
        frame = event.data or {}
        for t in frame.get("trades", []):
            payload = {"trade": t}
            is_new = await self._store.log_trade(market_id=market_id, payload=payload)
            if not is_new:
                self._tick_dedup_count += 1
                continue
            await self._dispatch(market_id, SseEvent(type="trade", seq=event.seq, data=payload))
        status = frame.get("status")
        prev = self._last_status.get(market_id)
        if status and status != prev:
            self._last_status[market_id] = status
            if prev:   # snapshot 未到过（prev 为空）不合成，等 bootstrap
                data = {"status": status}
                if frame.get("settlement"):
                    data.update(frame["settlement"])
                await self._dispatch(market_id, SseEvent(type="market_status",
                                                         seq=event.seq, data=data))

    async def _dispatch(self, market_id: int, event: SseEvent) -> None:
        for s in self._strategies:
            if getattr(s, "market_id", None) != market_id:
                continue
            try:
                await asyncio.wait_for(
                    s.on_sse_event(event),
                    timeout=self._dispatch_timeout_sec,
                )
            except asyncio.TimeoutError:
                self._log.error("sse_dispatch_timeout",
                                strategy=s.name, market_id=market_id,
                                timeout_sec=self._dispatch_timeout_sec)
            except Exception:
                self._log.exception("sse_on_sse_event_failed",
                                    strategy=s.name, market_id=market_id)
