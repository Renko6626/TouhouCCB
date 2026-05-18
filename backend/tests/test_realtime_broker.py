"""MarketEventBroker + IpConcurrencyLimiter 单元测试。

覆盖 fix/sse-broker-hardening 的三个改动：
1. Subscriber wrapper + kicked Event（dead consumer 不再静默卡死）
2. publish 慢消费者检测后 set kicked
3. IpConcurrencyLimiter try_acquire / release
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from app.services.realtime import (
    BROKER,
    IP_LIMITER,
    IpConcurrencyLimiter,
    MarketEventBroker,
    Subscriber,
)


# Override conftest 的 autouse setup_db：broker 是纯内存模块，不需要 DB 初始化。
# 不 override 会让每个测试卡在 SQLModel.metadata.drop_all/create_all 上 30s+。
@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    yield


# ─── BROKER subscribe/unsubscribe/publish 基本 ─────────────

@pytest.mark.asyncio
async def test_subscribe_returns_subscriber_with_event():
    """subscribe 返回 (Subscriber, anchor) tuple；Subscriber 含 queue + kicked Event。"""
    b = MarketEventBroker()
    sub, anchor = await b.subscribe(1)
    try:
        assert isinstance(sub, Subscriber)
        assert isinstance(sub.q, asyncio.Queue)
        assert isinstance(sub.kicked, asyncio.Event)
        assert not sub.kicked.is_set()
        assert b.subscriber_count(1) == 1
        assert anchor == 0  # fresh market, 没人 publish 过
    finally:
        await b.unsubscribe(1, sub)
        assert b.subscriber_count(1) == 0


@pytest.mark.asyncio
async def test_subscribe_anchor_reflects_current_seq():
    """先 publish 几笔再 subscribe → anchor = 最新 seq。"""
    b = MarketEventBroker()
    # 没订阅者时 publish 三笔 → seq 递增到 3 但没 q 接收
    await b.publish(1, "trade", {})
    await b.publish(1, "trade", {})
    await b.publish(1, "trade", {})
    sub, anchor = await b.subscribe(1)
    try:
        assert anchor == 3, f"anchor 应该是当前 seq=3, got {anchor}"
        # 现在 publish 第 4 笔，应该是新订阅者收到的第一个 event 且 seq=4
        await b.publish(1, "trade", {"i": 4})
        evt = await asyncio.wait_for(sub.q.get(), timeout=1.0)
        assert evt.seq == 4, "新 event seq 必须 > anchor"
        assert evt.seq > anchor
    finally:
        await b.unsubscribe(1, sub)


@pytest.mark.asyncio
async def test_subscribe_anchor_atomicity_under_concurrent_publish():
    """关键不变量：subscribe 返回 anchor 后，q 内事件 seq 严格 > anchor。

    模拟 high-frequency publish 期间订阅：先并发跑一堆 publish + 一个 subscribe，
    然后断言 anchor < min(q 内所有 event 的 seq)。
    """
    b = MarketEventBroker()

    async def publisher():
        for _ in range(200):
            await b.publish(1, "trade", {})
            await asyncio.sleep(0)  # let other coroutines run

    # 同时跑发布者 + 订阅者
    pub_task = asyncio.create_task(publisher())
    await asyncio.sleep(0.001)  # 让 publisher 跑几笔
    sub, anchor = await b.subscribe(1)

    # 等 publisher 完成
    await pub_task

    # 取 q 内所有 event 的 seq
    seqs_seen = []
    while not sub.q.empty():
        evt = sub.q.get_nowait()
        seqs_seen.append(evt.seq)

    # 关键断言：所有进 q 的 event seq 都 > anchor
    assert all(s > anchor for s in seqs_seen), \
        f"违反原子性！anchor={anchor}, q 内最小 seq={min(seqs_seen)}, " \
        f"应该全部 > anchor"

    await b.unsubscribe(1, sub)


@pytest.mark.asyncio
async def test_publish_to_subscriber_queue():
    """publish 应该把 event 推到 subscriber 的 q。"""
    b = MarketEventBroker()
    sub, _ = await b.subscribe(1)
    try:
        await b.publish(1, "trade", {"x": 1})
        evt = await asyncio.wait_for(sub.q.get(), timeout=1.0)
        assert evt.type == "trade"
        assert evt.market_id == 1
        assert evt.data == {"x": 1}
        assert evt.seq == 1
    finally:
        await b.unsubscribe(1, sub)


@pytest.mark.asyncio
async def test_publish_increments_seq_but_ping_does_not():
    b = MarketEventBroker()
    sub, _ = await b.subscribe(1)
    try:
        await b.publish(1, "trade", {})
        await b.publish(1, "trade", {})
        await b.publish(1, "ping", {})
        seqs = []
        for _ in range(3):
            evt = await asyncio.wait_for(sub.q.get(), timeout=1.0)
            seqs.append(evt.seq)
        # 两笔 trade seq 递增，ping 复用上一笔 seq
        assert seqs == [1, 2, 2]
    finally:
        await b.unsubscribe(1, sub)


@pytest.mark.asyncio
async def test_subscribe_respects_max_cap(monkeypatch):
    b = MarketEventBroker()
    monkeypatch.setattr(b, "MAX_SUBSCRIBERS_PER_MARKET", 2)
    s1, _ = await b.subscribe(1)
    s2, _ = await b.subscribe(1)
    try:
        with pytest.raises(RuntimeError, match="订阅者已满"):
            await b.subscribe(1)
    finally:
        await b.unsubscribe(1, s1)
        await b.unsubscribe(1, s2)


# ─── 关键：dead consumer kicked 行为 ──────────────────────

@pytest.mark.asyncio
async def test_publish_full_queue_kicks_subscriber():
    """publish 撑爆 q（不消费）→ 该 subscriber 应该被踢出 subs 且 kicked.set()。"""
    b = MarketEventBroker()
    # 测试加速：把 QUEUE_MAXSIZE 临时调小，省去发 2000 笔
    sub = Subscriber(q=asyncio.Queue(maxsize=3))
    async with b._lock:
        b._topics.setdefault(1, set()).add(sub)

    # publish 4 笔（容量 3）：第 4 笔触发 QueueFull → kick
    for i in range(4):
        await b.publish(1, "trade", {"i": i})

    # 验证：sub 被移除
    assert b.subscriber_count(1) == 0
    # kicked 已 set —— 这是 fix 的核心：generator 能立即感知
    assert sub.kicked.is_set()


@pytest.mark.asyncio
async def test_kicked_event_wakes_waiter():
    """sub.kicked 被 set 后，asyncio.wait 上等待它的协程应该立即被唤醒。
    这模拟 stream.py gen() 里 await asyncio.wait({get_task, kicked_task}) 的行为。"""
    sub = Subscriber(q=asyncio.Queue(maxsize=1))

    async def waiter():
        get_task = asyncio.create_task(sub.q.get())
        kicked_task = asyncio.create_task(sub.kicked.wait())
        done, pending = await asyncio.wait(
            {get_task, kicked_task},
            return_when=asyncio.FIRST_COMPLETED,
            timeout=2.0,
        )
        for t in pending:
            t.cancel()
        return kicked_task in done

    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)  # 让 waiter 跑到 wait
    sub.kicked.set()
    woken = await asyncio.wait_for(waiter_task, timeout=1.0)
    assert woken is True, "kicked Event 应该立即唤醒 wait"


@pytest.mark.asyncio
async def test_slow_consumer_does_not_starve_others():
    """混合场景：一个慢 consumer 被踢，另一个快 consumer 不受影响。"""
    b = MarketEventBroker()
    slow = Subscriber(q=asyncio.Queue(maxsize=2))
    fast = Subscriber(q=asyncio.Queue(maxsize=10))
    async with b._lock:
        b._topics.setdefault(1, set()).update({slow, fast})

    for i in range(5):
        await b.publish(1, "trade", {"i": i})

    # slow 被踢
    assert slow.kicked.is_set()
    # fast 没被踢，且收到全部 5 笔
    assert not fast.kicked.is_set()
    assert fast.q.qsize() == 5


# ─── IpConcurrencyLimiter ────────────────────────────────

@pytest.mark.asyncio
async def test_ip_limiter_basic_acquire_release():
    lim = IpConcurrencyLimiter()
    ok1 = await lim.try_acquire(1, "1.2.3.4")
    ok2 = await lim.try_acquire(1, "1.2.3.4")
    assert ok1 and ok2
    assert lim.count(1, "1.2.3.4") == 2
    await lim.release(1, "1.2.3.4")
    assert lim.count(1, "1.2.3.4") == 1
    await lim.release(1, "1.2.3.4")
    assert lim.count(1, "1.2.3.4") == 0


@pytest.mark.asyncio
async def test_ip_limiter_rejects_over_cap(monkeypatch):
    lim = IpConcurrencyLimiter()
    monkeypatch.setattr(lim, "MAX_PER_IP", 3)
    for _ in range(3):
        assert await lim.try_acquire(1, "1.1.1.1") is True
    # 第 4 次应拒
    assert await lim.try_acquire(1, "1.1.1.1") is False
    # 释放一个，再试可成功
    await lim.release(1, "1.1.1.1")
    assert await lim.try_acquire(1, "1.1.1.1") is True


@pytest.mark.asyncio
async def test_ip_limiter_isolates_per_market_per_ip():
    """市场 + IP 是独立的 key；不同 market 或 IP 互不影响。"""
    lim = IpConcurrencyLimiter()
    for _ in range(IpConcurrencyLimiter.MAX_PER_IP):
        assert await lim.try_acquire(1, "a") is True
    # 同 IP 不同 market：不受影响
    assert await lim.try_acquire(2, "a") is True
    # 同 market 不同 IP：不受影响
    assert await lim.try_acquire(1, "b") is True


@pytest.mark.asyncio
async def test_ip_limiter_release_unknown_is_safe():
    """release 一个从没 acquire 过的 (market, ip) 不应该崩溃也不会让计数变负。"""
    lim = IpConcurrencyLimiter()
    await lim.release(1, "x.x.x.x")
    assert lim.count(1, "x.x.x.x") == 0
