# app/services/realtime.py
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple


_logger = logging.getLogger(__name__)


@dataclass
class MarketEvent:
    type: str               # "snapshot" | "trade" | "market_status" | "ping"
    market_id: int
    ts: str                 # ISO UTC
    data: Dict[str, Any]
    # 单调递增 per-market 序号。客户端用来检测 gap：若 seq != lastSeq+1 → 触发
    # silent reconcile。snapshot 事件携带当前 seq 作为锚点；ping 复用最近一次
    # 真实事件的 seq（不增）。设为 0 表示"不参与 gap 检测"（用于向后兼容）。
    seq: int = 0


@dataclass(eq=False)
class Subscriber:
    """每个 SSE 连接对应一个 Subscriber。包含事件队列 + kicked 信号。

    kicked = publish() 检测到这个 queue 满（慢消费者）后会被 set；
    stream.py gen() 在 wait_for queue 或 kicked 任一就绪时即退出，
    避免 generator 卡在死队列上空转（旧 bug：踢出 subs 后 generator 仍
    await q.get() 等不到任何 trade event，只能靠 25s ping 假装活着）。

    eq=False 让 dataclass 退回默认 __eq__ / __hash__ = identity-based，
    这样实例可以放进 `_topics: set[Subscriber]`（每个实例是 unique key）。
    """
    q: asyncio.Queue
    kicked: asyncio.Event = field(default_factory=asyncio.Event)


class MarketEventBroker:
    """
    内存版 pubsub（单进程 OK，多进程会分裂）。
    """

    MAX_SUBSCRIBERS_PER_MARKET = 500
    QUEUE_MAXSIZE = 2000

    def __init__(self) -> None:
        self._topics: Dict[int, set[Subscriber]] = {}
        # per-market 序号计数器；publish 时持锁递增并写入 event.seq
        self._seq: Dict[int, int] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, market_id: int) -> Subscriber:
        async with self._lock:
            subs = self._topics.setdefault(market_id, set())
            if len(subs) >= self.MAX_SUBSCRIBERS_PER_MARKET:
                raise RuntimeError(f"市场 {market_id} 订阅者已满（上限 {self.MAX_SUBSCRIBERS_PER_MARKET}）")
            sub = Subscriber(q=asyncio.Queue(maxsize=self.QUEUE_MAXSIZE))
            subs.add(sub)
        return sub

    def current_seq(self, market_id: int) -> int:
        """当前 per-market 序号（无锁读取）。

        snapshot 事件用它作为客户端的 lastSeq 锚点：客户端记录此值，
        后续 event.seq 应该是 lastSeq+1、lastSeq+2 ...
        """
        return self._seq.get(market_id, 0)

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

    async def unsubscribe(self, market_id: int, sub: Subscriber) -> None:
        async with self._lock:
            s = self._topics.get(market_id)
            if not s:
                return
            s.discard(sub)
            if not s:
                self._topics.pop(market_id, None)

    async def publish(self, market_id: int, event_type: str, data: Dict[str, Any]) -> None:
        async with self._lock:
            # ping 不递增 seq（心跳不算"事件"，不参与 gap 检测）；其他类型递增
            if event_type != "ping":
                self._seq[market_id] = self._seq.get(market_id, 0) + 1
            seq = self._seq.get(market_id, 0)
            subs = list(self._topics.get(market_id, set()))

        evt = MarketEvent(
            type=event_type,
            market_id=market_id,
            ts=datetime.now(timezone.utc).isoformat(),
            data=data,
            seq=seq,
        )

        dead_subs = []
        for sub in subs:
            try:
                sub.q.put_nowait(evt)
            except asyncio.QueueFull:
                dead_subs.append(sub)
                _logger.warning(f"SSE queue full for market {market_id}, removing slow consumer")

        # 清理慢消费者，并 set kicked 让 generator 立即退出
        if dead_subs:
            async with self._lock:
                s = self._topics.get(market_id)
                if s:
                    for sub in dead_subs:
                        s.discard(sub)
            # set kicked 在 lock 外，避免多余持锁；Event.set 本身线程/协程安全
            for sub in dead_subs:
                sub.kicked.set()


BROKER = MarketEventBroker()


class IpConcurrencyLimiter:
    """Per-IP 并发 SSE 连接限制（防匿名 DDoS 把 MAX_SUBSCRIBERS_PER_MARKET 打满）。

    粒度 = (market_id, ip)。同一 IP 对同一市场最多 MAX_PER_IP 并发。
    nginx 走 X-Forwarded-For 时，调用方负责提取真实 client IP（取首段）。
    """
    MAX_PER_IP = 10

    def __init__(self) -> None:
        self._counts: Dict[Tuple[int, str], int] = {}
        self._lock = asyncio.Lock()

    async def try_acquire(self, market_id: int, ip: str) -> bool:
        """返回 True 表示获取到额度；False 表示已达上限拒绝新连接。"""
        async with self._lock:
            key = (market_id, ip)
            cur = self._counts.get(key, 0)
            if cur >= self.MAX_PER_IP:
                return False
            self._counts[key] = cur + 1
            return True

    async def release(self, market_id: int, ip: str) -> None:
        async with self._lock:
            key = (market_id, ip)
            cur = self._counts.get(key, 0)
            if cur <= 1:
                self._counts.pop(key, None)
            else:
                self._counts[key] = cur - 1

    def count(self, market_id: int, ip: str) -> int:
        """诊断用，无锁近似读。"""
        return self._counts.get((market_id, ip), 0)


IP_LIMITER = IpConcurrencyLimiter()


def sse_pack(evt: MarketEvent) -> str:
    payload = {
        "type": evt.type,
        "market_id": evt.market_id,
        "ts": evt.ts,
        "data": evt.data,
        "seq": evt.seq,
    }
    # SSE 格式：event + data（每条以 \n\n 结尾）
    return f"event: {evt.type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
