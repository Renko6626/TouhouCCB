"""WP4a focused tests: bounded, coalescing post-commit FX publisher.

No application lifespan and no database: the publisher's queue/coalescing and
lifecycle contract is exercised with injected publish hooks and the real
in-process broker.
"""
import asyncio
import json
from decimal import Decimal

import pytest

from app.services.fx import market_data, publisher
from app.services.fx.publisher import FxPublisher
from app.services.realtime import BROKER
from app.services.realtime import MarketEventBroker


def _prices(published):
    return [price for _, price, _ in published]


@pytest.mark.asyncio
async def test_enqueue_before_start_drops_and_spawns_no_task():
    instance = FxPublisher()
    before = len(asyncio.all_tasks())
    assert instance.enqueue(1, Decimal("1"), trade_id=1) is False
    assert instance.running is False
    assert instance.stats()["dropped"] == 1
    assert len(asyncio.all_tasks()) == before


@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_is_safe_when_never_started():
    instance = FxPublisher()
    await instance.stop()  # no-op
    await instance.start()
    task = instance._task
    await instance.start()
    assert instance._task is task  # exactly one worker task
    assert instance.running is True
    await instance.stop()
    await instance.stop()  # idempotent
    assert instance.running is False
    assert instance._task is None


@pytest.mark.asyncio
async def test_publication_is_coalesced_per_pair_to_the_latest_price():
    entered, release = asyncio.Event(), asyncio.Event()
    published = []

    async def hook(publication):
        published.append((publication.pair_id, publication.post_price,
                          publication.trade_id))
        entered.set()
        await release.wait()

    instance = FxPublisher(publish=hook)
    await instance.start()
    try:
        assert instance.enqueue(1, Decimal("1"), trade_id=1) is True
        await asyncio.wait_for(entered.wait(), 1)   # first frame is in flight
        for trade_id in range(2, 6):
            assert instance.enqueue(1, Decimal(trade_id), trade_id=trade_id) is True
        assert instance.stats()["coalesced"] == 4
        assert instance.queue_depth == 0            # coalesced, not queued
        release.set()
        await instance.drain(2)
        assert _prices(published) == [Decimal("1"), Decimal("5")]
        assert instance.stats()["published"] == 2
    finally:
        release.set()
        await instance.stop()


@pytest.mark.asyncio
async def test_queue_overflow_drops_without_blocking_and_keeps_pair_latest():
    entered, release = asyncio.Event(), asyncio.Event()
    published = []

    async def hook(publication):
        published.append((publication.pair_id, publication.post_price))
        if publication.pair_id == 1:
            entered.set()
            await release.wait()

    instance = FxPublisher(maxsize=2, publish=hook)
    await instance.start()
    try:
        assert instance.enqueue(1, Decimal("1")) is True   # in flight
        await asyncio.wait_for(entered.wait(), 1)
        assert instance.enqueue(2, Decimal("2")) is True   # queue slot 1
        assert instance.enqueue(3, Decimal("3")) is True   # queue slot 2 (full)
        # Producer is never blocked and never raises on overflow.
        assert instance.enqueue(4, Decimal("4")) is False
        assert instance.stats()["dropped"] == 1
        # A pair already queued still accepts a newer price while full.
        assert instance.enqueue(3, Decimal("3.5")) is True
        assert instance.stats()["coalesced"] == 1
        release.set()
        await instance.drain(2)
        assert published == [(1, Decimal("1")), (2, Decimal("2")), (3, Decimal("3.5"))]
    finally:
        release.set()
        await instance.stop()


@pytest.mark.asyncio
async def test_drain_waits_for_queue_and_pending_frames():
    published = []

    async def hook(publication):
        published.append((publication.pair_id, publication.post_price,
                          publication.trade_id))

    instance = FxPublisher(publish=hook)
    await instance.start()
    try:
        for trade_id in range(20):
            assert instance.enqueue(1, Decimal(trade_id), trade_id=trade_id) is True
        await instance.drain(2)
        assert instance.queue_depth == 0 and instance.pending_pairs == 0
        assert published[-1][1] == Decimal("19")
        stats = instance.stats()
        assert stats["enqueued"] + stats["coalesced"] + stats["dropped"] == 20
    finally:
        await instance.stop()


@pytest.mark.asyncio
async def test_stop_flushes_queued_frame_then_refuses_new_work():
    published = []

    async def hook(publication):
        published.append((publication.pair_id, publication.post_price))

    instance = FxPublisher(publish=hook, stop_drain_timeout=1)
    await instance.start()
    assert instance.enqueue(7, Decimal("1")) is True
    await instance.stop()

    assert published == [(7, Decimal("1"))]
    assert instance.running is False
    assert instance.enqueue(7, Decimal("2")) is False
    assert instance.stats()["dropped"] == 1


@pytest.mark.asyncio
async def test_publish_failure_is_counted_and_worker_keeps_running():
    calls = []

    async def hook(publication):
        calls.append(publication.pair_id)
        if publication.pair_id == 1:
            raise RuntimeError("boom")

    instance = FxPublisher(publish=hook)
    await instance.start()
    try:
        instance.enqueue(1, Decimal("1"))
        instance.enqueue(2, Decimal("2"))
        await instance.drain(2)
        assert calls == [1, 2]
        assert instance.stats()["failed"] == 1
        assert instance.running is True
    finally:
        await instance.stop()


@pytest.mark.asyncio
async def test_default_publish_skips_pair_without_subscribers(monkeypatch):
    calls = []

    async def fake_frame(pair_id, post_price, broker=None):
        calls.append((pair_id, post_price))

    monkeypatch.setattr(market_data, "publish_pair_frame", fake_frame)
    instance = FxPublisher()
    await instance.start()
    try:
        instance.enqueue(987654, Decimal("2.5"))
        await instance.drain(2)
        assert calls == []
        assert instance.stats()["skipped"] == 1
        assert instance.stats()["published"] == 0
    finally:
        await instance.stop()


@pytest.mark.asyncio
async def test_lmsr_viewer_with_same_id_does_not_trigger_fx_publication(monkeypatch):
    calls = []

    async def fake_frame(pair_id, post_price, broker=None):
        calls.append(pair_id)

    monkeypatch.setattr(market_data, "publish_pair_frame", fake_frame)
    broker = MarketEventBroker()
    subscriber, _ = await broker.subscribe("lmsr:987654")
    instance = FxPublisher(broker=broker)
    await instance.start()
    try:
        instance.enqueue(987654, Decimal("2.5"))
        await instance.drain(2)
        assert calls == []
        assert instance.stats()["skipped"] == 1
    finally:
        await instance.stop()
        await broker.unsubscribe("lmsr:987654", subscriber)


@pytest.mark.asyncio
async def test_default_publish_writes_wire_frame_when_subscribed(monkeypatch):
    async def fake_frame(pair_id, post_price, broker=None):
        await market_data.publish_public_frame(
            broker, pair_id, {"price": post_price, "volume": Decimal("4")})

    monkeypatch.setattr(market_data, "publish_pair_frame", fake_frame)
    sub, _anchor = await BROKER.subscribe(987655)
    instance = FxPublisher()
    await instance.start()
    try:
        instance.enqueue(987655, Decimal("3"))
        await instance.drain(2)
        blob = await asyncio.wait_for(sub.q.get(), 1)
        payload = json.loads(blob.decode().split("data: ", 1)[1])
        assert payload["market_id"] == 987655
        assert payload["type"] == "fx"
        assert payload["data"]["price"] == "3"
        assert instance.stats()["published"] == 1
    finally:
        await instance.stop()
        await BROKER.unsubscribe(987655, sub)


@pytest.mark.asyncio
async def test_module_level_lifecycle_hooks_drive_the_singleton(monkeypatch):
    instance = FxPublisher()
    monkeypatch.setattr(publisher, "PUBLISHER", instance)
    assert publisher.enqueue_publication(5, Decimal("1")) is False  # stopped

    await publisher.start_publisher()
    assert publisher.PUBLISHER is instance and instance.running is True
    await publisher.drain_publisher(1)
    await publisher.stop_publisher()
    assert instance.running is False
    assert publisher.publisher_stats()["dropped"] == 1
