import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from thccb_quant.client.sse import SseEvent
from thccb_quant.client.sse_subscriber import SseSubscriber
from thccb_quant.state.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    s = await Store.open(tmp_path / "t.db")
    yield s
    await s.close()


def _trade_event(seq, market_id, trade_id, outcome_id=1):
    return SseEvent(
        type="trade", seq=seq,
        data={"trade": {
            "id": trade_id, "type": "BUY", "outcome_id": outcome_id,
            "username": "u", "shares": 1.0, "price": 0.5,
            "gross": 0.5, "fee": 0.0, "post_market_price": 0.5,
            "market_prices_post": [0.5, 0.5],
            "timestamp": "2026-05-17T07:00:00Z",
        }},
    )


def _mk_strategy(name: str, market_id: int):
    s = MagicMock()
    s.name = name
    s.market_id = market_id
    s.on_sse_event = AsyncMock()
    return s


async def _make_sub(store, sse_events_per_market, strategies, market_ids):
    """sse_events_per_market: dict[market_id, list[SseEvent | Exception]]"""
    sse = MagicMock()
    async def subscribe(market_id):
        for ev in sse_events_per_market.get(market_id, []):
            if isinstance(ev, Exception):
                raise ev
            yield ev
    sse.subscribe = subscribe
    rest = MagicMock()
    rest.get_recent_trades = AsyncMock(return_value=[])
    return SseSubscriber(
        rest=rest, store=store, sse_client=sse,
        strategies=strategies, market_ids=market_ids,
        logger=structlog.get_logger("test"),
    )


async def test_trade_event_writes_table_and_dispatches(store):
    strat = _mk_strategy("s1", market_id=1)
    sub = await _make_sub(
        store,
        sse_events_per_market={1: [_trade_event(1, 1, trade_id=10)]},
        strategies=[strat],
        market_ids={1},
    )
    await asyncio.wait_for(sub.run(), timeout=2.0)
    rows = await store.recent_trades_observed(market_id=1, limit=10)
    assert len(rows) == 1
    assert rows[0]["trade_id"] == 10
    strat.on_sse_event.assert_called_once()


async def test_dispatch_routes_by_market_id(store):
    s1 = _mk_strategy("s1", market_id=1)
    s2 = _mk_strategy("s2", market_id=2)
    sub = await _make_sub(
        store,
        sse_events_per_market={1: [_trade_event(1, 1, trade_id=10)]},
        strategies=[s1, s2],
        market_ids={1},
    )
    await asyncio.wait_for(sub.run(), timeout=2.0)
    s1.on_sse_event.assert_called_once()
    s2.on_sse_event.assert_not_called()


async def test_strategy_exception_isolated(store):
    s1 = _mk_strategy("s1", market_id=1)
    s1.on_sse_event = AsyncMock(side_effect=RuntimeError("boom"))
    s2 = _mk_strategy("s2", market_id=1)
    sub = await _make_sub(
        store,
        sse_events_per_market={1: [_trade_event(1, 1, trade_id=10)]},
        strategies=[s1, s2],
        market_ids={1},
    )
    await asyncio.wait_for(sub.run(), timeout=2.0)
    s1.on_sse_event.assert_called_once()
    s2.on_sse_event.assert_called_once()


async def test_preload_partial_trades(store):
    sse = MagicMock()
    async def subscribe(market_id):
        return
        yield
    sse.subscribe = subscribe
    rest = MagicMock()
    rest.get_recent_trades = AsyncMock(return_value=[
        MagicMock(model_dump=lambda: {
            "id": 100, "outcome_id": 1, "type": "BUY", "shares": "1.0",
            "price": "0.5", "username": "u1",
            "timestamp": "2026-05-17T07:00:00Z",
            "market_id": 1, "market_title": "M", "outcome_label": "yes",
        }),
    ])
    sub = SseSubscriber(
        rest=rest, store=store, sse_client=sse,
        strategies=[], market_ids={1},
        logger=structlog.get_logger("test"),
    )
    await asyncio.wait_for(sub.run(), timeout=2.0)
    cur = await store._conn.execute("SELECT count(*) FROM partial_trades")
    n = (await cur.fetchone())[0]
    assert n == 1
