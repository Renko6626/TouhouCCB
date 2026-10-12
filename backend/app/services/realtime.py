"""进程内 SSE pubsub。LMSR 与 FX 使用独立的产品 topic、序号和 IP 限额。"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from app.services.credit.keys import symbol_namespace

_logger = logging.getLogger(__name__)
Topic = str


def _canonical_topic(topic: Topic) -> str:
    if not isinstance(topic, str):
        raise TypeError("realtime topic must be a 'product:id' string")
    product, sep, raw = topic.partition(":")
    if sep == ":" and product in ("lmsr", "fx") and raw.isdigit() and int(raw) > 0:
        return symbol_namespace(product, int(raw))
    raise ValueError(f"unknown realtime topic: {topic!r} (expected 'lmsr:<id>' or 'fx:<id>')")


def _topic_group_id(topic: Topic) -> int:
    return int(topic.partition(":")[2])


@dataclass
class MarketEvent:
    type: str               # "snapshot" | "tick" | "fx" | "ping"
    market_id: int          # wire 字段名保持历史；FX topic 下承载 pair_id
    ts: str                 # ISO UTC
    data: Dict[str, Any]
    # 单调递增 per-topic 序号。客户端用来检测 gap：若 seq != lastSeq+1 → 触发
    # silent reconcile。snapshot 事件携带当前 seq 作为锚点；ping 复用最近一次
    # 真实事件的 seq（不增）。新启动的 topic 尚无事件时为 0。
    seq: int = 0


@dataclass(eq=False)
class Subscriber:
    """每个 SSE 连接对应一个 Subscriber。包含事件队列 + kicked 信号。

    q 内元素是 publish() 打包好的 SSE wire 格式 bytes（`sse_pack(evt).encode()`），
    不是 MarketEvent 对象——同一条事件只序列化一次，N 个订阅者共享同一个 bytes
    对象，generator 直接转发（阶段 0 性能重构）。

    kicked = publish() 检测到这个 queue 满（慢消费者）后会被 set；
    stream.py gen() 在 wait_for queue 或 kicked 任一就绪时即退出，
    避免 generator 卡在死队列上空转（旧 bug：踢出 subs 后 generator 仍
    await q.get() 等不到任何行情帧，只能靠 25s ping 假装活着）。

    eq=False 让 dataclass 退回默认 __eq__ / __hash__ = identity-based，
    这样实例可以放进 `_topics: dict[str, set[Subscriber]]`（每个实例是 unique key）。
    """
    q: asyncio.Queue
    kicked: asyncio.Event = field(default_factory=asyncio.Event)


class MarketEventBroker:
    """
    内存版 pubsub（单进程 OK，多进程会分裂）。topic 为产品命名空间字符串。
    """

    MAX_SUBSCRIBERS_PER_MARKET = 500
    # 定频帧后队列深度 == "落后几帧"：32 帧 ≈ 4 s 落后容忍（spec § 5.2）。
    QUEUE_MAXSIZE = 32

    def __init__(self) -> None:
        self._topics: Dict[str, set[Subscriber]] = {}
        # per-topic 序号计数器；publish 时持锁递增并写入 event.seq
        self._seq: Dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, topic: Topic) -> Tuple["Subscriber", int]:
        """Register and capture this topic's seq under the same lock."""
        key = _canonical_topic(topic)
        async with self._lock:
            subs = self._topics.setdefault(key, set())
            if len(subs) >= self.MAX_SUBSCRIBERS_PER_MARKET:
                raise RuntimeError(f"市场 {key} 订阅者已满（上限 {self.MAX_SUBSCRIBERS_PER_MARKET}）")
            sub = Subscriber(q=asyncio.Queue(maxsize=self.QUEUE_MAXSIZE))
            subs.add(sub)
            anchor = self._seq.get(key, 0)
        return sub, anchor

    def current_seq(self, topic: Topic) -> int:
        """Read this topic's latest seq without acquiring the publication lock."""
        return self._seq.get(_canonical_topic(topic), 0)

    def subscriber_count(self, topic: Topic) -> int:
        """Approximate count for SSE admission; subscribe enforces the limit under lock."""
        return len(self._topics.get(_canonical_topic(topic), ()))

    async def unsubscribe(self, topic: Topic, sub: Subscriber) -> None:
        key = _canonical_topic(topic)
        async with self._lock:
            self._detach(sub, key)

    def _detach(self, sub: Subscriber, key: str) -> None:
        """Remove a subscriber while holding self._lock."""
        subs = self._topics.get(key)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                self._topics.pop(key, None)

    async def publish(self, topic: Topic, event_type: str, data: Dict[str, Any]) -> None:
        key = _canonical_topic(topic)
        group_id = _topic_group_id(key)
        async with self._lock:
            # ping 不递增 seq（心跳不算"事件"，不参与 gap 检测）；其他类型递增
            if event_type != "ping":
                self._seq[key] = self._seq.get(key, 0) + 1
            seq = self._seq.get(key, 0)
            subs = list(self._topics.get(key, set()))

        evt = MarketEvent(
            type=event_type,
            market_id=group_id,
            ts=datetime.now(timezone.utc).isoformat(),
            data=data,
            seq=seq,
        )

        blob = sse_pack(evt).encode("utf-8")   # ★ 整个进程只序列化一次（spec § 8 阶段 0）

        dead_subs = []
        for sub in subs:
            try:
                sub.q.put_nowait(blob)
            except asyncio.QueueFull:
                dead_subs.append(sub)
                _logger.warning(f"SSE queue full for topic {key}, removing slow consumer")

        # 清理慢消费者，并 set kicked 让 generator 立即退出
        if dead_subs:
            async with self._lock:
                for sub in dead_subs:
                    self._detach(sub, key)
            # set kicked 在 lock 外，避免多余持锁；Event.set 本身线程/协程安全
            for sub in dead_subs:
                sub.kicked.set()


BROKER = MarketEventBroker()


class IpConcurrencyLimiter:
    """Per-IP 并发 SSE 连接限制（防匿名 DDoS 把 MAX_SUBSCRIBERS_PER_MARKET 打满）。

    粒度 = (topic, ip)。同一 IP 对同一 topic 最多 MAX_PER_IP 并发。
    限流键按产品命名空间隔离：market 5 与 pair 5 各自计数，互不占用额度。
    nginx 走 X-Forwarded-For 时，调用方负责提取真实 client IP（取首段）。
    """
    MAX_PER_IP = 10

    def __init__(self) -> None:
        self._counts: Dict[Tuple[str, str], int] = {}
        self._lock = asyncio.Lock()

    async def try_acquire(self, topic: Topic, ip: str) -> bool:
        """返回 True 表示获取到额度；False 表示已达上限拒绝新连接。"""
        async with self._lock:
            key = (_canonical_topic(topic), ip)
            cur = self._counts.get(key, 0)
            if cur >= self.MAX_PER_IP:
                return False
            self._counts[key] = cur + 1
            return True

    async def release(self, topic: Topic, ip: str) -> None:
        async with self._lock:
            key = (_canonical_topic(topic), ip)
            cur = self._counts.get(key, 0)
            if cur <= 1:
                self._counts.pop(key, None)
            else:
                self._counts[key] = cur - 1

    def count(self, topic: Topic, ip: str) -> int:
        """诊断用，无锁近似读。"""
        return self._counts.get((_canonical_topic(topic), ip), 0)


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
