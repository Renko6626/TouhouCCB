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

class MarketEventBroker:
    """
    内存版 pubsub（单进程 OK，多进程会分裂）。
    """
    def __init__(self) -> None:
        self._topics: Dict[int, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, market_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        async with self._lock:
            self._topics.setdefault(market_id, set()).add(q)
        return q

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
        # 尽量不阻塞发布者：慢消费者就丢弃
        for q in subs:
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                pass

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
