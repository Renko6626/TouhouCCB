"""WP8a：realtime broker topic 产品命名空间 / seq / 订阅数 / IP 限流键隔离。

覆盖验收：
- ``lmsr:{id}`` 与 ``fx:{id}``（同号 ID）互不串流，seq / anchor / subscriber_count 独立；
- 公开 wire 帧形状不变（``{"type","market_id","ts","data","seq"}``）；
- 迁移期裸 int 兼容语义（publish 按事件类型归 lane、subscribe 未命名空间订阅）；
- ``fx.market_data.publish_public_frame`` 只进 fx lane、tick 帧只进 lmsr lane；
- ``IpConcurrencyLimiter`` 限流键按命名空间隔离。
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest
import pytest_asyncio

from app.services.realtime import (
    IpConcurrencyLimiter,
    MarketEventBroker,
)
from app.services.tick_broadcaster import TICK_BROADCASTER


# broker / limiter 是纯内存模块，不需要 DB setup（同 tests/test_realtime_broker.py）。
@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    yield


@pytest.fixture(autouse=True)
def _reset_tick_broadcaster():
    TICK_BROADCASTER._pending.clear()
    yield
    TICK_BROADCASTER._pending.clear()


def _parse_sse(blob: bytes) -> dict:
    text = blob.decode("utf-8")
    assert text.endswith("\n\n")
    data_line = next(line for line in text.split("\n") if line.startswith("data: "))
    return json.loads(data_line[len("data: "):])


def _drain(sub) -> list[dict]:
    events: list[dict] = []
    while not sub.q.empty():
        events.append(_parse_sse(sub.q.get_nowait()))
    return events


def _trade(trade_id: int = 1) -> dict:
    return {"id": trade_id, "type": "buy", "outcome_id": 11, "username": "alice",
            "shares": 1.0, "price": 0.5, "gross": 0.5, "fee": 0.0,
            "post_market_price": 0.51, "market_prices_post": [0.51, 0.49],
            "timestamp": "2026-08-21T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_namespaced_topics_never_cross_feed_same_numeric_id():
    """market 5 与 pair 5 是两个独立 topic：各自只收到自己的事件。"""
    broker = MarketEventBroker()
    lmsr_sub, _ = await broker.subscribe("lmsr:5")
    fx_sub, _ = await broker.subscribe("fx:5")
    try:
        await broker.publish("lmsr:5", "trade", {"src": "lmsr"})
        await broker.publish("fx:5", "fx", {"src": "fx"})
        assert [e["data"]["src"] for e in _drain(lmsr_sub)] == ["lmsr"]
        assert [e["data"]["src"] for e in _drain(fx_sub)] == ["fx"]
    finally:
        await broker.unsubscribe("lmsr:5", lmsr_sub)
        await broker.unsubscribe("fx:5", fx_sub)


@pytest.mark.asyncio
async def test_sequence_and_subscriber_counts_are_isolated_per_namespace():
    broker = MarketEventBroker()
    await broker.publish("lmsr:5", "trade", {})
    await broker.publish("lmsr:5", "trade", {})
    await broker.publish("fx:5", "fx", {})

    assert broker.current_seq("lmsr:5") == 2
    assert broker.current_seq("fx:5") == 1

    lmsr_sub, lmsr_anchor = await broker.subscribe("lmsr:5")
    fx_sub, fx_anchor = await broker.subscribe("fx:5")
    try:
        # anchor 是各自 lane 的 seq，不会因为另一个产品同号而漂移。
        assert (lmsr_anchor, fx_anchor) == (2, 1)
        assert broker.subscriber_count("lmsr:5") == 1
        assert broker.subscriber_count("fx:5") == 1

        await broker.publish("lmsr:5", "trade", {})
        await broker.publish("fx:5", "fx", {})
        assert _parse_sse(lmsr_sub.q.get_nowait())["seq"] == 3
        assert _parse_sse(fx_sub.q.get_nowait())["seq"] == 2
    finally:
        await broker.unsubscribe("lmsr:5", lmsr_sub)
        await broker.unsubscribe("fx:5", fx_sub)


@pytest.mark.asyncio
async def test_wire_frame_shape_preserved_with_numeric_market_id():
    broker = MarketEventBroker()
    fx_sub, _ = await broker.subscribe("fx:5")
    try:
        await broker.publish("fx:5", "fx", {"price": "1.25"})
        payload = _parse_sse(fx_sub.q.get_nowait())
        # 帧形状与历史一致（只换了 topic 分桶），market_id 仍承载数值 id。
        assert set(payload) == {"type", "market_id", "ts", "data", "seq"}
        assert payload["type"] == "fx"
        assert payload["market_id"] == 5
        assert payload["data"] == {"price": "1.25"}
    finally:
        await broker.unsubscribe("fx:5", fx_sub)


@pytest.mark.asyncio
async def test_new_style_subscriber_ignores_legacy_raw_int_publish_of_other_product():
    """裸 int + "fx" 事件进 fx lane：lmsr 命名空间订阅者收不到。"""
    broker = MarketEventBroker()
    lmsr_sub, _ = await broker.subscribe("lmsr:5")
    fx_sub, _ = await broker.subscribe("fx:5")
    try:
        await broker.publish(5, "fx", {"leak": True})
        assert _drain(lmsr_sub) == []
        assert [e["data"] for e in _drain(fx_sub)] == [{"leak": True}]

        await broker.publish(5, "trade", {"ok": True})
        assert [e["data"] for e in _drain(lmsr_sub)] == [{"ok": True}]
        assert _drain(fx_sub) == []
    finally:
        await broker.unsubscribe("lmsr:5", lmsr_sub)
        await broker.unsubscribe("fx:5", fx_sub)


@pytest.mark.asyncio
async def test_legacy_raw_int_subscription_covers_both_lanes_once():
    """裸 int 订阅是迁移期"未命名空间"订阅：两 lane 都收，但只算一个连接。"""
    broker = MarketEventBroker()
    sub, anchor = await broker.subscribe(5)
    try:
        assert anchor == 0
        assert broker.subscriber_count(5) == 1
        assert broker.subscriber_count("lmsr:5") == 1
        assert broker.subscriber_count("fx:5") == 1

        await broker.publish(5, "trade", {"n": 1})     # 裸 int + trade → lmsr lane
        await broker.publish("fx:5", "fx", {"n": 2})   # 命名 fx lane
        await broker.publish(5, "fx", {"n": 3})        # 裸 int + fx 事件 → fx lane
        assert [e["data"]["n"] for e in _drain(sub)] == [1, 2, 3]
    finally:
        await broker.unsubscribe(5, sub)
    # 退订必须把两个 lane 都摘干净，不然会留下永远收不到消费者的死连接。
    assert broker.subscriber_count("lmsr:5") == 0
    assert broker.subscriber_count("fx:5") == 0


@pytest.mark.asyncio
async def test_fx_public_frame_publishes_to_fx_lane_only():
    from app.services.fx.market_data import publish_public_frame

    broker = MarketEventBroker()
    lmsr_sub, _ = await broker.subscribe("lmsr:7")
    fx_sub, _ = await broker.subscribe("fx:7")
    try:
        await publish_public_frame(broker, 7, {"price": "1.1", "spread": "0.2", "volume": "3"})
        assert _drain(lmsr_sub) == []
        payloads = _drain(fx_sub)
        assert [p["type"] for p in payloads] == ["fx"]
        assert payloads[0]["market_id"] == 7
        # 遗留（裸 int）订阅者仍能看到 fx lane，fx.publisher 的订阅者预检也依赖这一点：
        # 裸 int 视图是两 lane 去重后的连接数（这里 lmsr/fx 各一个连接 = 2，非 0 即可发布）。
        assert broker.subscriber_count(7) == 2
    finally:
        await broker.unsubscribe("lmsr:7", lmsr_sub)
        await broker.unsubscribe("fx:7", fx_sub)


@pytest.mark.asyncio
async def test_tick_frame_publishes_to_lmsr_lane_only(monkeypatch):
    from app.services import realtime as realtime_module

    broker = MarketEventBroker()
    monkeypatch.setattr(realtime_module, "BROKER", broker)
    lmsr_sub, _ = await broker.subscribe("lmsr:7005")
    fx_sub, _ = await broker.subscribe("fx:7005")
    try:
        TICK_BROADCASTER.feed_trade(7005, [0.52, 0.48], _trade(1), "trading")
        assert await TICK_BROADCASTER.flush_once() == 1
        assert _drain(fx_sub) == []
        payloads = _drain(lmsr_sub)
        assert [p["type"] for p in payloads] == ["tick"]
        assert payloads[0]["market_id"] == 7005
        assert payloads[0]["data"]["prices"] == [0.52, 0.48]
    finally:
        await broker.unsubscribe("lmsr:7005", lmsr_sub)
        await broker.unsubscribe("fx:7005", fx_sub)


@pytest.mark.asyncio
async def test_ip_limiter_keys_are_namespaced(monkeypatch):
    limiter = IpConcurrencyLimiter()
    monkeypatch.setattr(limiter, "MAX_PER_IP", 2)
    ip = "1.1.1.1"
    assert await limiter.try_acquire("lmsr:5", ip)
    assert await limiter.try_acquire("lmsr:5", ip)
    assert not await limiter.try_acquire("lmsr:5", ip)
    # 同号 fx lane 不占用 lmsr 的额度。
    assert await limiter.try_acquire("fx:5", ip)
    assert limiter.count("lmsr:5", ip) == 2
    assert limiter.count("fx:5", ip) == 1
    # 裸 int 是 lmsr lane 的迁移期别名。
    assert limiter.count(5, ip) == 2
    assert not await limiter.try_acquire(5, ip)
    await limiter.release(5, ip)
    assert limiter.count("lmsr:5", ip) == 1
    assert limiter.count("fx:5", ip) == 1


@pytest.mark.asyncio
async def test_legacy_subscriber_count_sees_fx_lane_for_unmigrated_publisher_gate():
    """``fx.publisher`` 仍以裸 pair_id 预检订阅者（该文件不在 WP8a 所有权内）。

    裸 int 视图必须看得到 fx lane，否则所有 FX 帧都会被判成"无人观看"而跳过。
    """
    broker = MarketEventBroker()
    sub, _ = await broker.subscribe("fx:7")
    try:
        assert broker.subscriber_count(7) == 1
    finally:
        await broker.unsubscribe("fx:7", sub)
    assert broker.subscriber_count(7) == 0


@pytest.mark.asyncio
async def test_fx_publisher_delivers_frame_to_namespaced_subscriber(monkeypatch):
    """生产发布路径（FxPublisher → market_data → broker）落到 fx lane。"""
    from app.services.fx import market_data, publisher

    async def fake_frame(pair_id, post_price, broker=None):
        await market_data.publish_public_frame(
            broker, pair_id, {"price": post_price, "volume": Decimal("4")})

    monkeypatch.setattr(market_data, "publish_pair_frame", fake_frame)

    broker = MarketEventBroker()
    sub, _ = await broker.subscribe("fx:987655")
    instance = publisher.FxPublisher(broker=broker)
    await instance.start()
    try:
        assert instance.enqueue(987655, Decimal("3")) is True
        await instance.drain(2)
        payload = _parse_sse(await asyncio.wait_for(sub.q.get(), 1))
        assert payload["type"] == "fx"
        assert payload["market_id"] == 987655
        assert payload["data"]["price"] == "3"
        assert instance.stats()["published"] == 1
        assert instance.stats()["skipped"] == 0
    finally:
        await instance.stop()
        await broker.unsubscribe("fx:987655", sub)


@pytest.mark.asyncio
async def test_kicked_legacy_subscriber_is_detached_from_both_lanes(monkeypatch):
    """慢消费者踢出时两个 lane 都要摘掉（否则另一 lane 留下永久死连接）。"""
    broker = MarketEventBroker()
    monkeypatch.setattr(broker, "QUEUE_MAXSIZE", 2)
    sub, _ = await broker.subscribe(5)
    for _ in range(3):
        await broker.publish("lmsr:5", "trade", {})
    assert sub.kicked.is_set()
    assert broker.subscriber_count("lmsr:5") == 0
    assert broker.subscriber_count("fx:5") == 0


@pytest.mark.asyncio
async def test_invalid_topics_are_rejected():
    broker = MarketEventBroker()
    with pytest.raises(ValueError):
        await broker.subscribe("market:5")
    with pytest.raises(ValueError):
        await broker.publish("5", "trade", {})
    with pytest.raises(TypeError):
        await broker.publish(1.5, "trade", {})
    limiter = IpConcurrencyLimiter()
    with pytest.raises(ValueError):
        await limiter.try_acquire("fx:0", "1.1.1.1")
