# app/services/realtime.py
"""进程内 SSE pubsub + per-IP 并发限制。

WP8a：topic 从裸 int 改为**带产品前缀的字符串命名空间**
``"lmsr:{market_id}"`` / ``"fx:{pair_id}"``（``credit.keys.symbol_namespace``）。
LMSR market_id 与 FX pair_id 数值可能相同，命名空间是两者不互相串流的唯一保证：
``_topics``（订阅者）与 ``_seq``（gap 检测序号）都按规范 topic 分桶，
``IpConcurrencyLimiter`` 的限流键同样按 topic 隔离。

迁移期兼容（重要）：`market.py` / `market_writer.py` 等尚未迁移的调用点仍以裸 int
publish。裸 int 一律解析为 lmsr 命名空间（唯一例外：event_type == "fx" 时解析为 fx，
这是 ``fx.market_data.publish_public_frame`` 的旧形态）；裸 int subscribe 是
"未命名空间订阅"，会在同号的两个 lane 都注册，从而旧测试/旧调用点保持行为。
新代码必须显式传 ``symbol_namespace(...)``。

公开 wire 帧形状保持不变：``{"type","market_id","ts","data","seq"}``，其中
``market_id`` 对 FX topic 承载 pair_id（历史字段名，客户端按已订阅的 URL 解释产品）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, Union, cast

from app.services.credit.keys import Product, symbol_namespace

_logger = logging.getLogger(__name__)

# 规范 topic 字符串，或迁移期遗留的裸 int（见模块 docstring）。
Topic = Union[int, str]

_FX_EVENT_TYPE = "fx"


def _canonical_topic(topic: Topic, *, event_type: Optional[str] = None) -> str:
    """把 publish/subscribe/限流 topic 归一成 ``"lmsr:{id}"`` / ``"fx:{id}"``。

    裸 int 是迁移期遗留形态：publish 时按事件类型归入 lmsr（``"fx"`` 事件除外），
    其余场景（subscribe / current_seq / subscriber_count / 限流）归入 lmsr lane。
    字符串必须是严格的命名空间形式，避免新代码写出无法隔离的模糊 topic。
    """
    if isinstance(topic, bool) or not isinstance(topic, (int, str)):
        raise TypeError(f"realtime topic must be int or 'product:id' string, got {topic!r}")
    if isinstance(topic, int):
        product = cast(Product, _FX_EVENT_TYPE if event_type == _FX_EVENT_TYPE else "lmsr")
        return symbol_namespace(product, topic)
    product, sep, raw = topic.partition(":")
    if sep == ":" and product in ("lmsr", "fx") and raw.isdigit() and int(raw) > 0:
        return symbol_namespace(cast(Product, product), int(raw))
    raise ValueError(
        f"unknown realtime topic: {topic!r} (expected 'lmsr:<id>' or 'fx:<id>')"
    )


def _topic_group_id(topic: Topic) -> int:
    """wire 帧 ``market_id`` 的数值：命名空间里的 group id（LMSR market / FX pair）。"""
    if isinstance(topic, int) and not isinstance(topic, bool):
        return int(topic)
    return int(str(topic).partition(":")[2])


def _subscription_keys(topic: Topic) -> Tuple[str, ...]:
    """subscribe/unsubscribe 需要注册/摘除的规范 topic。

    裸 int 是"未命名空间订阅"：同时注册 lmsr 与 fx 两个 lane，让迁移期调用点
    （以及以裸 int 订阅的旧测试）不区分产品的语义保持。命名空间 topic 只有自己。
    """
    if isinstance(topic, int) and not isinstance(topic, bool):
        return (symbol_namespace("lmsr", topic), symbol_namespace("fx", topic))
    return (_canonical_topic(topic),)


class _TopicMap(dict):
    """``{规范 topic: 订阅者集合}``，读写时统一规范化键。

    存在的唯一理由：迁移期仍有代码/测试直接以裸 int 访问 ``BROKER._topics``
    （如 ``_topics.setdefault(1, set())``，对应 LMSR market 1）。裸 int 键经
    ``_canonical_topic`` 落到 lmsr lane，旧视角（"按 market_id 索引"）语义不变，
    内部字典则只保存字符串命名空间键。
    """

    def __getitem__(self, key: Topic) -> set:
        return dict.__getitem__(self, _canonical_topic(key))

    def __setitem__(self, key: Topic, value: set) -> None:
        dict.__setitem__(self, _canonical_topic(key), value)

    def __delitem__(self, key: Topic) -> None:
        dict.__delitem__(self, _canonical_topic(key))

    def __contains__(self, key: object) -> bool:
        return dict.__contains__(self, _canonical_topic(key))  # type: ignore[arg-type]

    def get(self, key: Topic, default: Any = None) -> Any:
        return dict.get(self, _canonical_topic(key), default)

    def setdefault(self, key: Topic, default: Any = None) -> Any:
        return dict.setdefault(self, _canonical_topic(key), default)

    def pop(self, key: Topic, *args: Any) -> Any:
        return dict.pop(self, _canonical_topic(key), *args)


@dataclass
class MarketEvent:
    type: str               # "snapshot" | "trade" | "tick" | "market_status" | "fx" | "ping"
    market_id: int          # wire 字段名保持历史；FX topic 下承载 pair_id
    ts: str                 # ISO UTC
    data: Dict[str, Any]
    # 单调递增 per-topic 序号。客户端用来检测 gap：若 seq != lastSeq+1 → 触发
    # silent reconcile。snapshot 事件携带当前 seq 作为锚点；ping 复用最近一次
    # 真实事件的 seq（不增）。设为 0 表示"不参与 gap 检测"（用于向后兼容）。
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
    await q.get() 等不到任何 trade event，只能靠 25s ping 假装活着）。

    topics = 本 subscriber 注册过的规范 topic 集合。裸 int（未命名空间）订阅会同时
    注册 lmsr/fx 两个 lane，踢出与退订必须两个都摘掉，否则另一个 lane 会留下
    永远收不到消费者的死连接。

    eq=False 让 dataclass 退回默认 __eq__ / __hash__ = identity-based，
    这样实例可以放进 `_topics: dict[str, set[Subscriber]]`（每个实例是 unique key）。
    """
    q: asyncio.Queue
    kicked: asyncio.Event = field(default_factory=asyncio.Event)
    topics: set[str] = field(default_factory=set)


class MarketEventBroker:
    """
    内存版 pubsub（单进程 OK，多进程会分裂）。topic 为产品命名空间字符串。
    """

    MAX_SUBSCRIBERS_PER_MARKET = 500
    # 定频帧后队列深度 == "落后几帧"：32 帧 ≈ 4 s 落后容忍（spec § 5.2）。
    # 迁移期（legacy_trade_events 开）队列里混有老事件，踢出判定比 4 s 更严格；
    # 阶段 5 关双发后回归纯帧语义。慢消费者踢出 + kicked 机制原样保留。
    QUEUE_MAXSIZE = 32

    def __init__(self) -> None:
        self._topics: Dict[str, set[Subscriber]] = _TopicMap()
        # per-topic 序号计数器；publish 时持锁递增并写入 event.seq
        self._seq: Dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, topic: Topic) -> Tuple["Subscriber", int]:
        """订阅 + 同锁内读 anchor seq，保证 q 内事件的 seq 全部 > 返回的 anchor。

        旧实现 anchor 在 subscribe 外另读，与 subscribe 之间存在微秒级 race window：
        publish 可能在两步之间发生，导致 q 收到一个 seq == anchor 的事件，client
        以为是 gap → reconnect → silent loss。

        现在 anchor 在 lock 内读：
        - 任何后续 publish 必须先获 lock → seq 必 > anchor，且 subs copy 包含我们 → 入 q
        - 任何之前 publish 的 subs copy 不含我们 → 不入 q，seq <= anchor

        Snapshot.data 可能包含一些 seq > anchor 事件的状态影响（如果它们在 DB read
        期间 commit），但这些事件**同样**会通过 q 推送，client 看到的是重复（不是漏失）。
        重复由 trader 侧用 trade_id 去重（store.log_trade INSERT OR IGNORE +
        sse_subscriber 跳过已存在事件的 dispatch）。

        裸 int 是迁移期"未命名空间订阅"：注册两个产品 lane，anchor 取两 lane 的最大
        seq（保守：任何入 q 事件仍严格 > anchor）。
        """
        keys = _subscription_keys(topic)
        async with self._lock:
            for key in keys:
                subs = self._topics.setdefault(key, set())
                if len(subs) >= self.MAX_SUBSCRIBERS_PER_MARKET:
                    raise RuntimeError(
                        f"市场 {key} 订阅者已满（上限 {self.MAX_SUBSCRIBERS_PER_MARKET}）"
                    )
            sub = Subscriber(q=asyncio.Queue(maxsize=self.QUEUE_MAXSIZE), topics=set(keys))
            for key in keys:
                self._topics.setdefault(key, set()).add(sub)
            anchor = max((self._seq.get(key, 0) for key in keys), default=0)
        return sub, anchor

    def current_seq(self, topic: Topic) -> int:
        """当前 per-topic 序号（无锁读取）。

        snapshot 事件用它作为客户端的 lastSeq 锚点：客户端记录此值，
        后续 event.seq 应该是 lastSeq+1、lastSeq+2 ...
        裸 int（迁移期）取两 lane 最大值。
        """
        keys = _subscription_keys(topic)
        return max((self._seq.get(key, 0) for key in keys), default=0)

    def subscriber_count(self, topic: Topic) -> int:
        """订阅者数（近似值，无锁读取）。

        用于 SSE 接入前的 503 预检：StreamingResponse 一旦开始就无法再发 503。
        无锁原因：(1) dict.get / set.__len__ 在 CPython 都是 GIL 原子操作，
        最差读到比真实值少/多 1 的瞬时值；(2) 给 publish() 的 hot path 减少
        一次潜在的 lock 等待——SSE 接入率远低于交易 publish 频率；
        (3) subscribe() 自身仍持锁做严格上限检查，预检漏掉的极罕见 race
        会落到 subscribe() 的 RuntimeError，由上层 generator 处理。

        裸 int 是迁移期"未命名空间"视图：返回同号两 lane 去重后的连接数
        （裸 int 订阅者在两 lane 各注册一次，这里只算一个连接）。
        `fx.publisher` 仍以裸 pair_id 做"有人订阅才发布"的预检，必须让它看到 fx lane。
        """
        if isinstance(topic, int) and not isinstance(topic, bool):
            keys = _subscription_keys(topic)
            return len(set().union(*(self._topics.get(key, set()) for key in keys)))
        return len(self._topics.get(_canonical_topic(topic), ()))

    async def unsubscribe(self, topic: Topic, sub: Subscriber) -> None:
        async with self._lock:
            self._detach(sub, (*_subscription_keys(topic), *sub.topics))

    def _detach(self, sub: Subscriber, keys: Tuple[str, ...]) -> None:
        """把 sub 从给定 topic 集合摘掉；空集合顺带删除。调用方必须持 self._lock。"""
        for key in set(keys):
            s = self._topics.get(key)
            if not s:
                continue
            s.discard(sub)
            if not s:
                self._topics.pop(key, None)

    async def publish(self, topic: Topic, event_type: str, data: Dict[str, Any]) -> None:
        key = _canonical_topic(topic, event_type=event_type)
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

        # 清理慢消费者（含其在另一个 lane 的注册），并 set kicked 让 generator 立即退出
        if dead_subs:
            async with self._lock:
                for sub in dead_subs:
                    self._detach(sub, (key, *sub.topics))
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
