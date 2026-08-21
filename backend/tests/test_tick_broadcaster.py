"""TickBroadcaster 单元测试：帧形状 / 合并 / 双发开关种子。"""
import json

import pytest
from sqlmodel import SQLModel

from app.core.database import engine, async_session_maker
from app.services.realtime import BROKER
from app.services.tick_broadcaster import TICK_BROADCASTER, legacy_events_enabled


def _parse_sse(blob: bytes) -> dict:
    text = blob.decode("utf-8")
    assert text.endswith("\n\n")
    data_line = next(l for l in text.split("\n") if l.startswith("data: "))
    return json.loads(data_line[len("data: "):])


@pytest.fixture(autouse=True)
def _reset_broadcaster():
    TICK_BROADCASTER._pending.clear()
    yield


def _trade(i=1):
    return {"id": i, "type": "buy", "outcome_id": 11, "username": "alice",
            "shares": 1.0, "price": 0.5, "gross": 0.5, "fee": 0.0,
            "post_market_price": 0.51, "market_prices_post": [0.51, 0.49],
            "timestamp": "2026-08-21T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_flush_sends_one_frame_and_drains():
    sub, _ = await BROKER.subscribe(7001)
    try:
        TICK_BROADCASTER.feed_trade(7001, [0.51, 0.49], _trade(1), "trading")
        TICK_BROADCASTER.feed_trade(7001, [0.52, 0.48], _trade(2), "trading")
        assert await TICK_BROADCASTER.flush_once() == 1
        payload = _parse_sse(sub.q.get_nowait())
        assert payload["type"] == "tick"
        assert payload["market_id"] == 7001
        assert payload["data"]["status"] == "trading"
        assert payload["data"]["prices"] == [0.52, 0.48]      # 帧价格取最后一笔
        assert [t["id"] for t in payload["data"]["trades"]] == [1, 2]  # 逐笔不丢
        assert "settlement" not in payload["data"]
        # 再 flush 必须 no-op（dirty 已清）
        assert await TICK_BROADCASTER.flush_once() == 0
        assert sub.q.empty()
    finally:
        await BROKER.unsubscribe(7001, sub)


@pytest.mark.asyncio
async def test_price_only_frame_has_empty_trades():
    sub, _ = await BROKER.subscribe(7002)
    try:
        TICK_BROADCASTER.feed_prices(7002, [0.6, 0.4], "trading")
        assert await TICK_BROADCASTER.flush_once() == 1
        payload = _parse_sse(sub.q.get_nowait())
        assert payload["data"]["trades"] == []
        assert payload["data"]["prices"] == [0.6, 0.4]
    finally:
        await BROKER.unsubscribe(7002, sub)


@pytest.mark.asyncio
async def test_settlement_carried_once():
    from app.models.base import MarketStatus
    sub, _ = await BROKER.subscribe(7003)
    try:
        TICK_BROADCASTER.feed_status(
            7003, MarketStatus.SETTLED,
            settlement={"winning_outcome_id": 11, "settled_at": "2026-08-21T01:00:00+00:00"},
            prices=[1.0, 0.0],
        )
        await TICK_BROADCASTER.flush_once()
        payload = _parse_sse(sub.q.get_nowait())
        assert payload["data"]["status"] == "settled"          # 枚举归一成小写值
        assert payload["data"]["settlement"]["winning_outcome_id"] == 11
        # settled 之后又来一笔 price feed → 下一帧不再带 settlement
        TICK_BROADCASTER.feed_prices(7003, [1.0, 0.0], "settled")
        await TICK_BROADCASTER.flush_once()
        payload2 = _parse_sse(sub.q.get_nowait())
        assert "settlement" not in payload2["data"]
    finally:
        await BROKER.unsubscribe(7003, sub)


@pytest.mark.asyncio
async def test_tick_shares_seq_counter_with_legacy_events():
    """迁移期不变式：legacy 事件与 tick 帧共用同一 per-market seq 计数器。"""
    sub, anchor = await BROKER.subscribe(7004)
    try:
        await BROKER.publish(7004, "trade", {"trade": _trade(9)})
        TICK_BROADCASTER.feed_trade(7004, [0.5, 0.5], _trade(9), "trading")
        await TICK_BROADCASTER.flush_once()
        legacy = _parse_sse(sub.q.get_nowait())
        tick = _parse_sse(sub.q.get_nowait())
        assert legacy["seq"] == anchor + 1
        assert tick["seq"] == anchor + 2       # 连续，无跳号
    finally:
        await BROKER.unsubscribe(7004, sub)


@pytest.mark.asyncio
async def test_legacy_flag_seeded_default_true():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    from app.services.loan_migrate import auto_migrate
    from app.services import site_config
    site_config.clear_cache()
    await auto_migrate()
    assert await legacy_events_enabled() is True
    async with async_session_maker() as s:
        assert await site_config.get_str(s, "legacy_trade_events") == "true"
