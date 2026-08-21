"""tick 帧适配器：帧→合成逐笔事件，策略零改动。"""
import asyncio

import pytest

from thccb_quant.client.sse import SseEvent
from thccb_quant.client.sse_subscriber import SseSubscriber


class FakeStore:
    def __init__(self):
        self.logged: list[dict] = []
        self.known_ids: set[int] = set()

    async def log_trade(self, *, market_id, payload):
        tid = int(payload["trade"]["id"])
        if tid in self.known_ids:
            return False
        self.known_ids.add(tid)
        self.logged.append(payload)
        return True

    async def bulk_insert_partial_trades(self, items):
        return 0


class FakeStrategy:
    name = "fake"
    market_id = 1

    def __init__(self):
        self.events: list[SseEvent] = []

    async def on_sse_event(self, event):
        self.events.append(event)


def _subscriber(store, strat):
    import structlog
    return SseSubscriber(rest=None, store=store, sse_client=None,
                         strategies=[strat], market_ids=[1],
                         logger=structlog.get_logger("test"))


def _tick(seq, trades, status="trading", settlement=None):
    data = {"status": status, "prices": [0.5, 0.5], "trades": trades}
    if settlement:
        data["settlement"] = settlement
    return SseEvent(type="tick", seq=seq, data=data)


def _t(i):
    return {"id": i, "type": "buy", "outcome_id": 1, "username": "u",
            "shares": 1.0, "price": 0.5, "gross": 0.5, "fee": 0.0,
            "post_market_price": 0.5, "market_prices_post": [0.5, 0.5],
            "timestamp": "2026-08-21T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_tick_trades_dispatched_as_synthetic_trade_events():
    store, strat = FakeStore(), FakeStrategy()
    sub = _subscriber(store, strat)
    await sub._handle_event(1, _tick(5, [_t(1), _t(2)]))
    assert [e.type for e in strat.events] == ["trade", "trade"]
    assert [e.data["trade"]["id"] for e in strat.events] == [1, 2]
    assert len(store.logged) == 2


@pytest.mark.asyncio
async def test_tick_dedups_against_legacy_events_silently():
    """双发期：legacy 事件先到已入库 → 帧内同 id 静默跳过，不派发第二次。"""
    store, strat = FakeStore(), FakeStrategy()
    sub = _subscriber(store, strat)
    await sub._handle_event(1, SseEvent(type="trade", seq=4, data={"trade": _t(1)}))
    await sub._handle_event(1, _tick(5, [_t(1)]))
    assert [e.data["trade"]["id"] for e in strat.events if e.type == "trade"] == [1]
    assert sub.dedup_skipped_count == 0            # 帧内重复不污染异常告警计数器
    assert sub.tick_dedup_count == 1


@pytest.mark.asyncio
async def test_tick_status_change_emits_synthetic_market_status_once():
    store, strat = FakeStore(), FakeStrategy()
    sub = _subscriber(store, strat)
    await sub._handle_event(1, SseEvent(type="snapshot", seq=1,
                                        data={"status": "trading", "outcomes": []}))
    await sub._handle_event(1, _tick(2, []))                       # 状态没变 → 不合成
    await sub._handle_event(1, _tick(3, [], status="halt"))        # 变了 → 合成一次
    await sub._handle_event(1, _tick(4, [], status="halt"))        # 没再变 → 不再合成
    ms = [e for e in strat.events if e.type == "market_status"]
    assert len(ms) == 1
    assert ms[0].data["status"] == "halt"


@pytest.mark.asyncio
async def test_tick_settled_carries_settlement_fields():
    store, strat = FakeStore(), FakeStrategy()
    sub = _subscriber(store, strat)
    await sub._handle_event(1, SseEvent(type="snapshot", seq=1,
                                        data={"status": "trading", "outcomes": []}))
    await sub._handle_event(1, _tick(2, [], status="settled",
                                     settlement={"winning_outcome_id": 9,
                                                 "settled_at": "2026-08-21T01:00:00+00:00"}))
    ms = [e for e in strat.events if e.type == "market_status"]
    assert ms[0].data["winning_outcome_id"] == 9
