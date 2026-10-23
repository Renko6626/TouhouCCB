import asyncio
import json

import pytest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.services.realtime import IpConcurrencyLimiter, MarketEventBroker
from app.services.fx.market_data import publish_public_frame, publish_trade


@pytest.fixture(autouse=True)
def _stub_history_reader_for_existing_wire_tests(monkeypatch):
    from app.api.v1 import fx_stream
    from fastapi import HTTPException

    async def read(db, pair_id, now):
        runtime = fx_stream.FX_MARKET_DATA
        tail = runtime.tail(pair_id, now) if hasattr(runtime, "tail") else None
        if tail is None:
            raise HTTPException(status_code=503, detail="FX history is not ready")
        return tail

    monkeypatch.setattr(fx_stream, "read_fx_stream_history", read)
    # These wire/queue tests stub the quote; real row-lock behavior lives in PG.
    async def pair_state(db, pair_id):
        return SimpleNamespace(id=pair_id)
    monkeypatch.setattr(fx_stream.trading, "read_public_pair_state", pair_state)



@pytest.mark.asyncio
async def test_fx_public_stream_uses_broker_and_disconnects_kicked_subscriber():
    broker = MarketEventBroker()
    sub, _ = await broker.subscribe('fx:3')
    try:
        await publish_public_frame(broker, 3, {"price": "1.1", "spread": "0.2", "volume": "4"})
        payload = json.loads((await sub.q.get()).decode().split("data: ", 1)[1])
        assert payload["type"] == "fx"
        assert payload["data"] == {"price": "1.1", "spread": "0.2", "volume": "4"}

        sub.kicked.set()
        get_task = asyncio.create_task(sub.q.get())
        kicked_task = asyncio.create_task(sub.kicked.wait())
        done, pending = await asyncio.wait({get_task, kicked_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        assert kicked_task in done
    finally:
        await broker.unsubscribe('fx:3', sub)


@pytest.mark.asyncio
async def test_fx_stream_snapshot_serializes_decimal_values_and_cleans_up_on_disconnect(monkeypatch):
    from app.api.v1 import fx_stream

    class FakeDB:
        async def execute(self, statement):
            return None

        async def commit(self):
            return None

        async def get(self, model, pair_id):
            return SimpleNamespace(id=pair_id)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeMaker:
        def __call__(self):
            return FakeDB()

    snapshot = {
        "price": Decimal("1.10"), "buy_price": Decimal("1.11"),
        "sell_price": Decimal("1.09"), "spread": Decimal("0.02"),
        "volume_24h": Decimal("4.00"),
    }
    monkeypatch.setattr(fx_stream, "async_session_maker", FakeMaker())
    async def fake_snapshot(db, pair_id, *, pair_state):
        return snapshot
    monkeypatch.setattr(fx_stream.trading, "get_public_snapshot", fake_snapshot)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="test"))

    broker = MarketEventBroker()
    limiter = IpConcurrencyLimiter()
    monkeypatch.setattr(fx_stream, "BROKER", broker)
    monkeypatch.setattr(fx_stream, "IP_LIMITER", limiter)
    waiting = asyncio.Event()
    child_tasks = set()
    original_wait = asyncio.wait

    async def wait(tasks, **kwargs):
        child_tasks.update(tasks)
        waiting.set()
        return await original_wait(tasks, **kwargs)

    monkeypatch.setattr(fx_stream.asyncio, "wait", wait)
    response = await fx_stream.stream(91, request)
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    body = await response.body_iterator.__anext__()
    payload = json.loads(body.decode().split("data: ", 1)[1])
    assert payload["type"] == "snapshot"
    assert payload["data"]["price"] == "1.10"
    assert payload["data"]["spread"] == "0.02"
    next_event = asyncio.create_task(response.body_iterator.__anext__())
    await asyncio.wait_for(waiting.wait(), timeout=1)
    assert broker.subscriber_count("fx:91") == 1
    assert limiter.count("fx:91", "test") == 1
    next_event.cancel()
    with pytest.raises(asyncio.CancelledError):
        await next_event
    assert len(child_tasks) == 2
    assert all(task.done() for task in child_tasks)
    assert broker.subscriber_count("fx:91") == 0
    assert limiter.count("fx:91", "test") == 0
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_fx_stream_subscribes_before_snapshot(monkeypatch):
    from app.api.v1 import fx_stream

    order = []
    broker = MarketEventBroker()
    original_subscribe = broker.subscribe

    async def subscribe(pair_id):
        order.append("subscribe")
        return await original_subscribe(pair_id)

    monkeypatch.setattr(fx_stream, "BROKER", broker)
    monkeypatch.setattr(broker, "subscribe", subscribe)

    class FakeDB:
        async def execute(self, statement):
            return None

        async def commit(self):
            return None

        async def get(self, model, pair_id):
            return SimpleNamespace(id=pair_id)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False

    class FakeMaker:
        def __call__(self):
            return FakeDB()

    monkeypatch.setattr(fx_stream, "async_session_maker", FakeMaker())
    async def fake_snapshot(db, pair_id, *, pair_state):
        order.append("snapshot")
        # A trade committed while the snapshot is being read must remain in
        # the subscriber queue even when the returned snapshot is stale.
        await broker.publish(f"fx:{pair_id}", "fx", {"price": "2", "volume": "1"})
        return {"price": Decimal("1"), "volume_24h": Decimal("0")}
    monkeypatch.setattr(fx_stream.trading, "get_public_snapshot", fake_snapshot)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="test"))
    response = await fx_stream.stream(92, request)
    initial = json.loads((await response.body_iterator.__anext__()).decode().split("data: ", 1)[1])
    published = json.loads((await response.body_iterator.__anext__()).decode().split("data: ", 1)[1])
    assert order == ["subscribe", "snapshot"]
    assert initial["seq"] == 0
    assert initial["data"]["price"] == "1"
    assert published["seq"] == 1
    assert published["data"]["price"] == "2"
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_publish_trade_uses_committed_snapshot_quotes_and_cumulative_volume(monkeypatch):
    from app.services.fx import market_data, trading
    from app.core import database
    broker = MarketEventBroker()
    sub, _ = await broker.subscribe('fx:4')
    class FakeDB:
        async def execute(self, statement):
            return None

        async def commit(self):
            return None

        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    class FakeMaker:
        def __call__(self): return FakeDB()
    monkeypatch.setattr(database, "async_session_maker", FakeMaker())
    async def fake_snapshot(db, pair_id, *, pair_state):
        return {"price": Decimal("1.2"), "buy_price": Decimal("1.3"),
                "sell_price": Decimal("1.1"), "spread": Decimal("0.2"),
                "volume_24h": Decimal("9.5")}
    monkeypatch.setattr(trading, "get_public_snapshot", fake_snapshot)
    trade = SimpleNamespace(pair_id=4, post_price=Decimal("1.25"), side="buy",
                            input_amount=Decimal("2"), output_amount=Decimal("1"))
    try:
        await publish_trade(trade, broker)
        payload = json.loads((await sub.q.get()).decode().split("data: ", 1)[1])
        assert payload["data"] == {"price": "1.25", "buy_price": "1.3",
                                    "sell_price": "1.1", "spread": "0.2", "volume": "9.5"}
    finally:
        await broker.unsubscribe('fx:4', sub)


@pytest.mark.asyncio
async def test_publish_trade_without_broker_only_enqueues_on_the_bounded_publisher(monkeypatch):
    from app.services.fx import market_data, publisher

    accepted = []

    def enqueue(pair_id, post_price, *, trade_id=None):
        accepted.append((pair_id, post_price, trade_id))
        return True

    async def direct(*args, **kwargs):
        raise AssertionError("production publication must not write to the broker directly")

    monkeypatch.setattr(publisher, "enqueue_publication", enqueue)
    monkeypatch.setattr(market_data, "publish_pair_frame", direct)

    trade = SimpleNamespace(pair_id=7, post_price=Decimal("1.25"), id=99)
    await market_data.publish_trade(trade)
    assert accepted == [(7, Decimal("1.25"), 99)]


def test_public_frame_to_wire_recurses_while_retaining_the_allowlist():
    from app.services.fx.market_data import build_public_envelope, public_frame_to_wire

    frame = build_public_envelope(
        {"price": Decimal("1.10"), "volume_24h": Decimal("4"), "target_price": Decimal("9")},
        history={
            "history_version": "V", "history_ready": True,
            "history_tail_through_event_id": 7,
            "history_tail": {"1m": {
                "t0": 0, "step": 60, "n_buckets": 60, "t": [0],
                "o": [Decimal("1.10")], "h": [Decimal("1.20")], "l": [Decimal("1.05")],
                "c": [Decimal("1.15")], "v": [Decimal("2")], "trades": [1],
                "private": "leak",
            }},
        },
        trades=[{"id": 3, "ts": "2026-10-01T00:00:00+00:00",
                 "pre_price": Decimal("1.10"), "post_price": Decimal("1.11"), "gold_volume": Decimal("2.5"),
                 "user_id": 42, "target_price": "leak"}],
    )
    wire = public_frame_to_wire(frame)
    assert wire["price"] == "1.10"
    assert "target_price" not in wire
    assert wire["history_version"] == "V"
    assert wire["history_tail_through_event_id"] == 7
    assert wire["history_tail"]["1m"]["o"] == ["1.10"]
    assert "private" not in wire["history_tail"]["1m"]
    assert wire["trades"][0] == {"id": 3, "ts": "2026-10-01T00:00:00+00:00",
                                 "pre_price": "1.10", "post_price": "1.11", "gold_volume": "2.5"}


class _StubRuntime:
    def __init__(self, *, trades=(), invalidated=False, state=None, write_owner=True):
        self.write_owner = write_owner
        self._trades = list(trades)
        self._invalidated = invalidated
        self._state = state
        self.caught_up: list[int] = []

    async def catch_up(self, pair_id):
        self.caught_up.append(pair_id)
        return 0

    def drain_public_events(self, pair_id):
        return list(self._trades), self._invalidated

    def state(self, pair_id):
        return dict(self._state) if self._state else None


def _patch_publish(monkeypatch, runtime):
    from app.core import database
    from app.services.fx import market_state, market_data, trading

    class FakeDB:
        async def execute(self, statement):
            return None

        async def commit(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(database, "async_session_maker", lambda: FakeDB())
    async def fake_snapshot(db, pair_id, *, pair_state):
        return {"price": Decimal("1.2"), "volume_24h": Decimal("9.5")}
    monkeypatch.setattr(trading, "get_public_snapshot", fake_snapshot)
    monkeypatch.setattr(market_state, "FX_MARKET_DATA", runtime)
    return market_data


@pytest.mark.asyncio
async def test_publish_pair_frame_drains_deltas_once_after_owner_catch_up(monkeypatch):
    from app.services.fx.publisher import FxPublisher

    stub = _StubRuntime(
        trades=[{"id": 2, "ts": "2026-10-01T00:00:00+00:00",
                 "post_price": "1.10", "gold_volume": "1.5"},
                {"id": 3, "ts": "2026-10-01T00:00:00+00:00",
                 "post_price": "1.11", "gold_volume": "2.5"}],
        state={"history_version": "V", "history_ready": True,
               "applied_event_id": 3, "durable_event_id": 3},
    )
    _patch_publish(monkeypatch, stub)
    broker = MarketEventBroker()
    sub, _ = await broker.subscribe('fx:5')
    instance = FxPublisher(broker=broker)
    await instance.start()
    try:
        instance.enqueue(5, Decimal("1.10"), trade_id=2)
        instance.enqueue(5, Decimal("1.25"), trade_id=3)
        await instance.drain(2)
        payload = json.loads((await sub.q.get()).decode().split("data: ", 1)[1])
        data = payload["data"]
        assert data["price"] == "1.2"
        assert data["volume"] == "9.5"
        assert data["history_version"] == "V"
        assert data["history_tail_through_event_id"] == 3
        assert [event["id"] for event in data["events"]] == [2, 3]
        assert [event["gold_volume"] for event in data["events"]] == ["1.5", "2.5"]
        assert stub.caught_up == [5]
    finally:
        await instance.stop()
        await broker.unsubscribe('fx:5', sub)


@pytest.mark.asyncio
async def test_publish_pair_frame_surfaces_buffer_overflow_as_invalidation(monkeypatch):
    stub = _StubRuntime(trades=[], invalidated=True,
                        state={"history_version": "V", "history_ready": True,
                               "applied_event_id": 9, "durable_event_id": 9})
    market_data = _patch_publish(monkeypatch, stub)
    broker = MarketEventBroker()
    sub, _ = await broker.subscribe('fx:6')
    try:
        await market_data.publish_pair_frame(6, Decimal("1.25"), broker=broker)
        data = json.loads((await sub.q.get()).decode().split("data: ", 1)[1])["data"]
        assert data["history_invalidated"] is True
        assert "trades" not in data
    finally:
        await broker.unsubscribe('fx:6', sub)


@pytest.mark.asyncio
async def test_fx_stream_snapshot_carries_history_tail_and_coverage_cursor(monkeypatch):
    from app.api.v1 import fx_stream

    class FakeDB:
        async def execute(self, statement):
            return None

        async def commit(self):
            return None

        async def get(self, model, pair_id):
            return SimpleNamespace(id=pair_id)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeMaker:
        def __call__(self):
            return FakeDB()

    class TailRuntime:
        def __init__(self):
            self.caught_up: list[int] = []

        def state(self, pair_id):
            return {"history_version": "V", "history_ready": True,
                    "applied_event_id": 12, "durable_event_id": 12}

        async def catch_up(self, pair_id):
            self.caught_up.append(pair_id)
            return 13

        def tail(self, pair_id, now):
            return {
                "history_version": "V", "history_ready": True,
                "history_tail": {"1m": {
                    "t0": 0, "step": 60, "n_buckets": 60, "t": [0],
                    "o": [Decimal("1.10")], "h": [Decimal("1.20")],
                    "l": [Decimal("1.05")], "c": [Decimal("1.15")],
                    "v": [Decimal("2")], "trades": [1],
                }},
                "history_tail_at": "2026-10-01T00:00:00+00:00",
                "history_tail_through_event_id": 12,
            }

    runtime = TailRuntime()
    monkeypatch.setattr(fx_stream, "async_session_maker", FakeMaker())
    monkeypatch.setattr(fx_stream, "FX_MARKET_DATA", runtime)
    async def fake_snapshot(db, pair_id, *, pair_state):
        return {"price": Decimal("1.10"), "spread": Decimal("0.02"),
                "volume_24h": Decimal("4"), "target_price": Decimal("9")}
    monkeypatch.setattr(fx_stream.trading, "get_public_snapshot", fake_snapshot)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="test"))

    response = await fx_stream.stream(93, request)
    payload = json.loads((await response.body_iterator.__anext__()).decode().split("data: ", 1)[1])
    data = payload["data"]
    assert data["price"] == "1.10"
    assert data["history_version"] == "V"
    assert data["history_tail_through_event_id"] == 12
    assert data["history_tail"]["1m"]["c"] == ["1.15"]
    assert "target_price" not in data
    # A ready runtime is caught up before the tail so a just-committed trade
    # cannot be omitted from the first snapshot.
    assert runtime.caught_up == []
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_fx_stream_snapshot_does_not_backfill_unknown_pair(monkeypatch):
    """A GET must not create/backfill derived state for an unknown pair."""
    from app.api.v1 import fx_stream

    class FakeDB:
        async def execute(self, statement):
            return None

        async def commit(self):
            return None

        async def get(self, model, pair_id):
            return SimpleNamespace(id=pair_id)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeMaker:
        def __call__(self):
            return FakeDB()

    class UnknownRuntime:
        def __init__(self):
            self.catch_up_calls = 0

        def state(self, pair_id):
            return None

        async def catch_up(self, pair_id):
            self.catch_up_calls += 1
            return 0

        def tail(self, pair_id, now):
            return None

    runtime = UnknownRuntime()
    monkeypatch.setattr(fx_stream, "async_session_maker", FakeMaker())
    monkeypatch.setattr(fx_stream, "FX_MARKET_DATA", runtime)
    async def fake_snapshot(db, pair_id, *, pair_state):
        return {"price": Decimal("1.10"), "volume_24h": Decimal("4")}
    monkeypatch.setattr(fx_stream.trading, "get_public_snapshot", fake_snapshot)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="test"))

    response = await fx_stream.stream(94, request)
    payload = json.loads((await response.body_iterator.__anext__()).decode().split("data: ", 1)[1])
    assert "history_version" not in payload["data"]
    assert runtime.catch_up_calls == 0
    await response.body_iterator.aclose()
