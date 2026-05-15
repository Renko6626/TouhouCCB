# app/services/realtime.py
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

@dataclass
class MarketEvent:
    type: str               # "snapshot" | "trade" | "market_status" | "ping"
    market_id: int
    ts: str                 # ISO UTC
    data: Dict[str, Any]

import logging

_logger = logging.getLogger(__name__)


class MarketEventBroker:
    """
    内存版 pubsub（单进程 OK，多进程会分裂）。
    """

    MAX_SUBSCRIBERS_PER_MARKET = 500
    QUEUE_MAXSIZE = 2000

    def __init__(self) -> None:
        self._topics: Dict[int, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, market_id: int) -> asyncio.Queue:
        async with self._lock:
            subs = self._topics.setdefault(market_id, set())
            if len(subs) >= self.MAX_SUBSCRIBERS_PER_MARKET:
                raise RuntimeError(f"市场 {market_id} 订阅者已满（上限 {self.MAX_SUBSCRIBERS_PER_MARKET}）")
            q: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_MAXSIZE)
            subs.add(q)
        return q

    def subscriber_count(self, market_id: int) -> int:
        """订阅者数（近似值，无锁读取）。

        用于 SSE 接入前的 503 预检：StreamingResponse 一旦开始就无法再发 503。
        无锁原因：(1) dict.get / set.__len__ 在 CPython 都是 GIL 原子操作，
        最差读到比真实值少/多 1 的瞬时值；(2) 给 publish() 的 hot path 减少
        一次潜在的 lock 等待——SSE 接入率远低于交易 publish 频率；
        (3) subscribe() 自身仍持锁做严格上限检查，预检漏掉的极罕见 race
        会落到 subscribe() 的 RuntimeError，由上层 generator 处理。
        """
        return len(self._topics.get(market_id, ()))

    async def unsubscribe(self, market_id: int, q: asyncio.Queue) -> None:
        async with self._lock:
            s = self._topics.get(market_id)
            if not s:
                return
            s.discard(q)
            if not s:
                self._topics.pop(market_id, None)

    async def publish(self, market_id: int, event_type: str, data: Dict[str, Any]) -> None:
        evt = MarketEvent(
            type=event_type,
            market_id=market_id,
            ts=datetime.now(timezone.utc).isoformat(),
            data=data,
        )
        async with self._lock:
            subs = list(self._topics.get(market_id, set()))

        dead_queues = []
        for q in subs:
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                dead_queues.append(q)
                _logger.warning(f"SSE queue full for market {market_id}, removing slow consumer")

        # 清理慢消费者
        if dead_queues:
            async with self._lock:
                s = self._topics.get(market_id)
                if s:
                    for q in dead_queues:
                        s.discard(q)

BROKER = MarketEventBroker()

def sse_pack(evt: MarketEvent) -> str:
    payload = {
        "type": evt.type,
        "market_id": evt.market_id,
        "ts": evt.ts,
        "data": evt.data,
    }
    # SSE 格式：event + data（每条以 \n\n 结尾）
    return f"event: {evt.type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
