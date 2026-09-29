"""WP8a：``/market/{id}` 与 ``/fx/stream/{id}`` 同号 ID 的端点级隔离。

用真实端点 generator（非 HTTP 流，沿用 tests/test_stream_*.py 的模式）验证：
- 两个端点分别订阅 ``lmsr:{id}`` / ``fx:{id}``，同号互不串流；
- 交叉 publish 只到各自 lane（trade 不进 FX 流、fx 帧不进 market 流、tick 帧不进 FX 流）；
- 订阅计数与 per-IP 限流额度按命名空间分桶，断流后全部释放。
"""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.api.v1 import fx_stream
from app.api.v1 import stream as market_stream
from app.core.database import async_session_maker
from app.models.base import Market, MarketStatus, Outcome
from app.models.fx import FxPair
from app.services import realtime as realtime_module
from app.services.realtime import IpConcurrencyLimiter, MarketEventBroker
from app.services.tick_broadcaster import TICK_BROADCASTER


@pytest_asyncio.fixture(autouse=True)
async def _reset_tick_broadcaster():
    TICK_BROADCASTER._pending.clear()
    yield
    TICK_BROADCASTER._pending.clear()


async def _seed_same_id_market_and_pair() -> int:
    """建一个 market 与一个 pair，``FxPair.id`` 显式等于 ``Market.id``（同号场景）。"""
    async with async_session_maker() as db:
        market = Market(title="t", description="", liquidity_b=100.0,
                        status=MarketStatus.TRADING, tags="")
        db.add(market)
        await db.flush()
        mid = int(market.id)
        db.add(Outcome(market_id=mid, label="a", total_shares=Decimal("0")))
        db.add(Outcome(market_id=mid, label="b", total_shares=Decimal("0")))
        db.add(FxPair(id=mid, currency_code=f"G{mid}", currency_name="Gold",
                      status="trading", gold_reserve=Decimal("100"),
                      foreign_reserve=Decimal("100")))
        await db.commit()
    return mid


async def _next_frame(response) -> dict:
    raw = await response.body_iterator.__anext__()
    text = raw.decode()
    return json.loads(text.split("data: ", 1)[1])


@pytest.mark.asyncio
async def test_same_numeric_id_market_and_pair_streams_are_isolated(monkeypatch):
    broker = MarketEventBroker()
    limiter = IpConcurrencyLimiter()
    # tick_broadcaster 在 flush 时从 realtime 模块懒取 BROKER，必须一起打补丁，
    # 否则 tick 帧会发到全局 broker、market 流永远等不到（25s 后只发 ping）。
    monkeypatch.setattr(realtime_module, "BROKER", broker)
    monkeypatch.setattr(market_stream, "BROKER", broker)
    monkeypatch.setattr(market_stream, "IP_LIMITER", limiter)
    monkeypatch.setattr(fx_stream, "BROKER", broker)
    monkeypatch.setattr(fx_stream, "IP_LIMITER", limiter)

    mid = await _seed_same_id_market_and_pair()
    request_m = SimpleNamespace(headers={}, client=SimpleNamespace(host="10.1.1.1"))
    request_f = SimpleNamespace(headers={}, client=SimpleNamespace(host="10.1.1.2"))

    response_m = await market_stream.stream_market(mid, request_m)
    response_f = await fx_stream.stream(mid, request_f)
    try:
        first_m = await _next_frame(response_m)
        first_f = await _next_frame(response_f)
        assert first_m["type"] == "snapshot"
        assert first_f["type"] == "snapshot"
        assert first_m["market_id"] == first_f["market_id"] == mid

        # 订阅计数 / seq / 限流键都按命名空间分桶。
        assert broker.subscriber_count(f"lmsr:{mid}") == 1
        assert broker.subscriber_count(f"fx:{mid}") == 1
        assert broker.current_seq(f"lmsr:{mid}") == 0
        assert broker.current_seq(f"fx:{mid}") == 0
        assert limiter.count(f"lmsr:{mid}", "10.1.1.1") == 1
        assert limiter.count(f"fx:{mid}", "10.1.1.2") == 1

        # 交叉发布：FX 帧不进 market 流，trade 帧不进 FX 流。
        await broker.publish(f"fx:{mid}", "fx", {"price": "2.0"})
        await broker.publish(f"lmsr:{mid}", "trade", {"trade": {"id": 1}})
        second_m = await _next_frame(response_m)
        second_f = await _next_frame(response_f)
        assert second_m["type"] == "trade"
        assert second_f["type"] == "fx"
        assert second_f["data"]["price"] == "2.0"

        # tick 帧只进 lmsr lane：FX 流下一个帧是哨兵业务帧（若 tick 串流会先读到 tick）。
        TICK_BROADCASTER.feed_trade(mid, [0.7, 0.3], {"id": 2}, "trading")
        assert await TICK_BROADCASTER.flush_once() == 1
        await broker.publish(f"fx:{mid}", "fx", {"price": "3.0"})
        third_m = await _next_frame(response_m)
        third_f = await _next_frame(response_f)
        assert third_m["type"] == "tick"
        assert third_m["data"]["prices"] == [0.7, 0.3]
        assert third_f["type"] == "fx"
        assert third_f["data"]["price"] == "3.0"
    finally:
        await response_m.body_iterator.aclose()
        await response_f.body_iterator.aclose()

    # 断流后订阅与限流额度都释放（退订必须两个 lane 都干净）。
    assert broker.subscriber_count(f"lmsr:{mid}") == 0
    assert broker.subscriber_count(f"fx:{mid}") == 0
    assert limiter.count(f"lmsr:{mid}", "10.1.1.1") == 0
    assert limiter.count(f"fx:{mid}", "10.1.1.2") == 0
