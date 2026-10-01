import asyncio
import json

import pytest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.services.realtime import MarketEventBroker
from app.services.fx.market_data import publish_public_frame, publish_trade


@pytest.mark.asyncio
async def test_fx_public_stream_uses_broker_and_disconnects_kicked_subscriber():
    broker = MarketEventBroker()
    sub, _ = await broker.subscribe(3)
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
        await broker.unsubscribe(3, sub)


@pytest.mark.asyncio
async def test_fx_stream_snapshot_serializes_decimal_values(monkeypatch):
    from app.api.v1 import fx_stream

    class FakeDB:
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
    async def fake_snapshot(db, pair_id):
        return snapshot
    monkeypatch.setattr(fx_stream.trading, "get_public_snapshot", fake_snapshot)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="test"))

    response = await fx_stream.stream(91, request)
    body = await response.body_iterator.__anext__()
    payload = json.loads(body.decode().split("data: ", 1)[1])
    assert payload["type"] == "snapshot"
    assert payload["data"]["price"] == "1.10"
    assert payload["data"]["spread"] == "0.02"
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
    async def fake_snapshot(db, pair_id):
        order.append("snapshot")
        # A trade committed while the snapshot is being read must remain in
        # the subscriber queue even when the returned snapshot is stale.
        await broker.publish(pair_id, "fx", {"price": "2", "volume": "1"})
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
    sub, _ = await broker.subscribe(4)
    class FakeDB:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    class FakeMaker:
        def __call__(self): return FakeDB()
    monkeypatch.setattr(database, "async_session_maker", FakeMaker())
    async def fake_snapshot(db, pair_id):
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
        await broker.unsubscribe(4, sub)


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
