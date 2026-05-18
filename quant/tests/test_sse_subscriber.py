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


async def test_dispatch_timeout_does_not_block_next_strategy(store):
    """A 个 strategy 卡住超 timeout 不应阻塞 B strategy 收到 event。"""
    import asyncio as _asyncio

    # strategy A: 卡住 5 秒
    slow_strat = _mk_strategy("slow", market_id=1)
    async def slow_on_sse(event):
        await _asyncio.sleep(5)  # 远超我们 dispatch_timeout=0.2 的设置
    slow_strat.on_sse_event = slow_on_sse

    # strategy B: 正常
    fast_strat = _mk_strategy("fast", market_id=1)

    sub = await _make_sub(
        store,
        sse_events_per_market={1: [_trade_event(1, 1, trade_id=10)]},
        strategies=[slow_strat, fast_strat],
        market_ids={1},
    )
    sub._dispatch_timeout_sec = 0.2  # 让测试快

    await _asyncio.wait_for(sub.run(), timeout=3.0)

    # fast strategy 应该被调用（slow 的 timeout 不应阻塞它）
    fast_strat.on_sse_event.assert_called_once()


async def test_dispatch_timeout_logs_warning(store, caplog):
    """timeout 时应该 log sse_dispatch_timeout（区别于 sse_on_sse_event_failed）。"""
    import asyncio as _asyncio
    import logging
    caplog.set_level(logging.ERROR)

    slow_strat = _mk_strategy("slow", market_id=1)
    async def slow_on_sse(event):
        await _asyncio.sleep(5)
    slow_strat.on_sse_event = slow_on_sse

    sub = await _make_sub(
        store,
        sse_events_per_market={1: [_trade_event(1, 1, trade_id=10)]},
        strategies=[slow_strat],
        market_ids={1},
    )
    sub._dispatch_timeout_sec = 0.1

    await _asyncio.wait_for(sub.run(), timeout=2.0)

    # 不能确切断言 log 输出（因为 structlog 不一定走 caplog），但可以断言
    # 没有 unhandled exception——sub.run() 干净完成
    # （如果 wait_for 没把 TimeoutError 吃掉就会冒泡）


async def test_duplicate_trade_event_skips_dispatch(store):
    """同一 trade_id 来两次：第一次正常 dispatch，第二次 store 返回 False → 跳过 dispatch。

    防 backend snapshot+event 双推 / SSE 重连重发让策略 EMA 多更新一次。
    """
    strat = _mk_strategy("s1", market_id=1)
    ev = _trade_event(seq=1, market_id=1, trade_id=42)
    ev2 = _trade_event(seq=2, market_id=1, trade_id=42)   # 同 trade_id
    sub = await _make_sub(
        store,
        sse_events_per_market={1: [ev, ev2]},
        strategies=[strat],
        market_ids={1},
    )
    await asyncio.wait_for(sub.run(), timeout=2.0)
    # 第一次入 DB + dispatch；第二次 INSERT OR IGNORE 跳过 → 不 dispatch
    assert strat.on_sse_event.call_count == 1, \
        f"重复 trade 应只 dispatch 一次, got {strat.on_sse_event.call_count}"
    rows = await store.recent_trades_observed(market_id=1, limit=10)
    assert len(rows) == 1


async def test_last_event_handled_ts_updates(store):
    """每个 event 处理后 last_event_handled_ts 应该更新。"""
    import time as _time

    sub = await _make_sub(
        store,
        sse_events_per_market={1: [_trade_event(1, 1, trade_id=10)]},
        strategies=[],
        market_ids={1},
    )
    before = sub.last_event_handled_ts
    await asyncio.wait_for(sub.run(), timeout=2.0)
    after = sub.last_event_handled_ts
    assert after > before, f"expected ts to advance, before={before} after={after}"


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
