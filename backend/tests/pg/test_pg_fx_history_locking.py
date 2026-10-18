"""PostgreSQL cross-boundary lock test for the immutable FX history proof.

A producer holds ``FOR UPDATE`` on the pair row while its ``FxTrade`` (born
inside a closed segment) is still uncommitted.  A concurrent cache-miss
``/history`` read must wait on the shared lock, and after the producer commits
it must refuse the segment (503) because the durable cursor is still behind —
never cache an immutable segment that is missing the trade.  A rolled-back
producer must release the lock and let the empty sealed segment be served.

Requires ``TEST_PG_DATABASE_URL`` pointing at this task's disposable database
(``fx_md_api_test``); skipped otherwise.  Never pointed at another worker's DB.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.fx import FxMarketDataState, FxPair, FxTrade
from app.services.fx.market_reads import clear_fx_history_cache, read_fx_history
from app.services.fx.market_state import FX_MARKET_DATA
from app.services.fx.pool_events import append_pool_event

pytestmark = [pytest.mark.pg, pytest.mark.asyncio]

UTC = timezone.utc


async def _seed_pair(factory) -> int:
    async with factory() as db:
        pair = FxPair(currency_code="PGH", currency_name="PG History")
        db.add(pair)
        await db.flush()
        pid = int(pair.id)
        db.add(FxMarketDataState(pair_id=pid,
                                 history_version="gen-1", history_ready=True))
        await db.commit()
        return pid


def _previous_hour() -> int:
    now = int(datetime.now(UTC).timestamp())
    return now - (now % 3600) - 3600


def _trade(pid: int, trade_id: int, when: datetime) -> FxTrade:
    return FxTrade(
        id=trade_id, pair_id=pid, side="buy",
        input_amount=Decimal("2"), output_amount=Decimal("2"),
        pre_gold_reserve=Decimal("100"), pre_foreign_reserve=Decimal("100"),
        post_gold_reserve=Decimal("100"), post_foreign_reserve=Decimal("100"),
        post_price=Decimal("1.10"), created_at=when,
    )


@pytest.fixture(autouse=True)
def _reset_runtime():
    FX_MARKET_DATA.reset()
    clear_fx_history_cache()
    yield
    FX_MARKET_DATA.reset()
    clear_fx_history_cache()


async def test_history_read_waits_for_inflight_producer_and_refuses_after_commit(pg_sessionmaker):
    pid = await _seed_pair(pg_sessionmaker)
    seg = _previous_hour()
    trade_at = datetime.fromtimestamp(seg + 120, tz=UTC)

    producer = pg_sessionmaker()
    await producer.execute(select(FxPair.id).where(FxPair.id == pid).with_for_update())
    trade = _trade(pid, 1, trade_at)
    producer.add(trade)
    await producer.flush()  # establish the receipt FK inside the same transaction
    append_pool_event(producer, pair_id=pid, kind="trade", created_at=trade_at,
                      pre_reserves=(trade.pre_gold_reserve, trade.pre_foreign_reserve),
                      post_reserves=(trade.post_gold_reserve, trade.post_foreign_reserve),
                      gold_volume=trade.input_amount, trade_id=trade.id)
    await producer.flush()  # uncommitted; the pair row lock is held

    reader = pg_sessionmaker()
    task = asyncio.create_task(read_fx_history(reader, pid, "gen-1", "1m", seg))
    try:
        # While the producer is in flight the reader cannot pass the shared
        # lock, so it can never prove-and-cache a segment missing that trade.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)

        await producer.commit()
        with pytest.raises(HTTPException) as exc:
            await asyncio.wait_for(task, timeout=5)
        assert exc.value.status_code == 503
    finally:
        await producer.rollback()
        await producer.close()
        if not task.done():
            task.cancel()
        await reader.close()


async def test_history_read_serves_after_inflight_producer_rolls_back(pg_sessionmaker):
    pid = await _seed_pair(pg_sessionmaker)
    seg = _previous_hour()

    producer = pg_sessionmaker()
    await producer.execute(select(FxPair.id).where(FxPair.id == pid).with_for_update())
    trade = _trade(pid, 2, datetime.fromtimestamp(seg + 120, tz=UTC))
    producer.add(trade)
    await producer.flush()  # establish the receipt FK inside the same transaction
    append_pool_event(producer, pair_id=pid, kind="trade", created_at=trade.created_at,
                      pre_reserves=(trade.pre_gold_reserve, trade.pre_foreign_reserve),
                      post_reserves=(trade.post_gold_reserve, trade.post_foreign_reserve),
                      gold_volume=trade.input_amount, trade_id=trade.id)
    await producer.flush()

    reader = pg_sessionmaker()
    task = asyncio.create_task(read_fx_history(reader, pid, "gen-1", "1m", seg))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)

        await producer.rollback()
        enc = await asyncio.wait_for(task, timeout=5)
        assert enc["t0"] == seg
        assert enc["v"] == []   # the rolled-back trade never existed
    finally:
        await producer.rollback()
        await producer.close()
        if not task.done():
            task.cancel()
        await reader.close()


@pytest.mark.parametrize('consumer', ['initial', 'broadcast'])
async def test_public_frame_waits_for_row_and_reads_commit_before_memory_install(
        pg_engine, pg_sessionmaker, monkeypatch, consumer):
    import json
    import os
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from app.api.v1 import fx_stream
    from app.core import database
    from app.services import trading_prices
    from app.services.credit import flags, ownership
    from app.services.fx import trading
    from app.services.fx import writer as fx_writer
    from app.services.fx.market_data import publish_pair_frame
    from app.services.realtime import IpConcurrencyLimiter, MarketEventBroker
    from tests.pg.test_pg_fx_short_concurrency import seed

    owner = ownership.WriteOwnership(url=os.environ['TEST_PG_DATABASE_URL'])
    assert await owner.acquire(required=True)
    monkeypatch.setattr(trading, 'OWNERSHIP', owner)
    monkeypatch.setattr(trading_prices, 'OWNERSHIP', owner)
    monkeypatch.setattr(fx_writer, 'OWNERSHIP', owner)
    registry = trading_prices.PricingRegistry(pg_sessionmaker)
    registry.configure(memory_products=frozenset({'fx'}))
    monkeypatch.setattr(trading_prices, 'REGISTRY', registry)
    monkeypatch.setattr(database, 'async_session_maker', pg_sessionmaker)
    monkeypatch.setattr(fx_stream, 'async_session_maker', pg_sessionmaker)
    broker = MarketEventBroker()
    monkeypatch.setattr(fx_stream, 'BROKER', broker)
    monkeypatch.setattr(fx_stream, 'IP_LIMITER', IpConcurrencyLimiter())
    flags.clear_flags()
    before_commit, release_commit = asyncio.Event(), asyncio.Event()
    committed, release_install = asyncio.Event(), asyncio.Event()
    producer = reading = None
    response = sub = None
    try:
        uid, pid = await seed(pg_sessionmaker)
        await fx_writer.FX_WRITER.start()
        old = await registry.read_fx(pid)

        @asynccontextmanager
        async def execution_session():
            async with pg_sessionmaker() as db:
                original = db.commit
                async def held_commit():
                    before_commit.set()
                    await release_commit.wait()
                    await original()
                    committed.set()
                    await release_install.wait()
                if asyncio.current_task().get_name().startswith('fx-writer-'):
                    monkeypatch.setattr(db, 'commit', held_commit)
                yield db

        monkeypatch.setattr(database, 'async_session_maker', execution_session)

        async def write():
            async with pg_sessionmaker() as db:
                return await trading.execute_trade(db, uid, pid, 'buy', Decimal('10'),
                                                   Decimal('0'), 'public-commit-gap')

        producer = asyncio.create_task(write())
        await asyncio.wait_for(before_commit.wait(), 5)
        if consumer == 'initial':
            request = SimpleNamespace(headers={}, client=SimpleNamespace(host='pg-history'))
            response = await fx_stream.stream(pid, request)
            reading = asyncio.create_task(response.body_iterator.__anext__())
        else:
            sub, _ = await broker.subscribe(f'fx:{pid}')
            reading = asyncio.create_task(publish_pair_frame(pid, Decimal('1'), broker))
        # Both paths retain FOR SHARE: an uncommitted trade cannot be combined
        # with a supposedly committed historical prefix.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(reading), .1)
        release_commit.set()
        await asyncio.wait_for(committed.wait(), 5)
        blob = await asyncio.wait_for(reading, 5)
        if consumer == 'broadcast':
            blob = await asyncio.wait_for(sub.q.get(), 5)
        data = json.loads(blob.decode().split('data: ', 1)[1])['data']
        assert Decimal(data['volume']) == Decimal('10')
        # Exclusive GATE remains held: readers complete from their locked DB
        # row while memory is still the prior complete committed snapshot.
        assert registry.peek_fx(pid) is old and old.pool_version == 1
        async with pg_sessionmaker() as db:
            pair = await db.get(FxPair, pid)
            assert pair.pool_version == 2
            from app.services.fx.amm import quote_buy
            expected = quote_buy(Decimal('1'), pair.gold_reserve,
                                 pair.foreign_reserve, pair.buy_fee_rate)
            ask = (expected.input_amount / expected.output_amount).quantize(Decimal('.00000001'))
        assert Decimal(data['buy_price']) == ask
        assert data.get('history_ready', False) is False
        release_install.set()
        await asyncio.wait_for(producer, 5)
        assert registry.peek_fx(pid).pool_version == 2
    finally:
        release_commit.set()
        release_install.set()
        for task in (producer, reading):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (producer, reading) if task is not None),
                             return_exceptions=True)
        if response is not None:
            await response.body_iterator.aclose()
        if sub is not None:
            await broker.unsubscribe(f'fx:{pid}', sub)
        flags.clear_flags()
        await fx_writer.FX_WRITER.stop()
        await owner.release()


async def test_locked_public_fallback_is_fresh_and_never_installs(pg_sessionmaker, monkeypatch):
    from sqlalchemy import update
    from app.schemas.trading_context import GroupKey
    from app.services import trading_prices
    from app.services.credit import flags
    from app.services.fx import trading
    from app.services.market_read_state import FxReadState

    pid = await _seed_pair(pg_sessionmaker)
    registry = trading_prices.PricingRegistry(pg_sessionmaker)
    registry.configure(memory_products=frozenset({'fx'}))
    monkeypatch.setattr(trading_prices, 'REGISTRY', registry)
    async with pg_sessionmaker() as db:
        pair = await db.get(FxPair, pid)
        old = FxReadState.from_pair(pair)
        registry.install_committed(old)
        # Maintenance SQL uses DB mode. Retain an old identity-map row to
        # verify the locked fallback refreshes current committed fields.
        monkeypatch.setenv(flags.READ_ONLY_ENV, 'true')
        async with pg_sessionmaker() as writer:
            await writer.execute(update(FxPair).where(FxPair.id == pid).values(
                pool_version=2, gold_reserve=Decimal('200')))
            await writer.commit()
        fresh = await trading.read_public_pair_state(db, pid)
        assert fresh.pool_version == 2 and fresh.gold_reserve == Decimal('200')
        assert pair.pool_version == 2
        await db.commit()
        monkeypatch.delenv(flags.READ_ONLY_ENV)
        assert registry._entries[('fx', pid)].state is old
        registry.invalidate(GroupKey(product='fx', id=pid))
        assert (await trading.read_public_pair_state(db, pid)).pool_version == 2
        assert not registry._entries
        await db.commit()
        with pytest.raises(HTTPException) as exc:
            await trading.read_public_pair_state(db, pid + 1000)
        assert exc.value.status_code == 404
        assert not registry._entries


async def test_initial_read_only_connection_keeps_quote_without_history_proof(
        pg_sessionmaker, monkeypatch):
    import json
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from sqlalchemy import text
    from app.api.v1 import fx_stream
    from app.services import trading_prices
    from app.services.market_read_state import FxReadState
    from app.services.realtime import IpConcurrencyLimiter, MarketEventBroker

    pid = await _seed_pair(pg_sessionmaker)
    registry = trading_prices.PricingRegistry(pg_sessionmaker)
    async with pg_sessionmaker() as db:
        registry.install_committed(FxReadState.from_pair(await db.get(FxPair, pid)))
    monkeypatch.setattr(trading_prices, 'REGISTRY', registry)
    sessions = []
    @asynccontextmanager
    async def read_only():
        async with pg_sessionmaker() as db:
            sessions.append(db)
            await db.execute(text('SET TRANSACTION READ ONLY'))
            yield db
    monkeypatch.setattr(fx_stream, 'async_session_maker', read_only)
    broker, limiter = MarketEventBroker(), IpConcurrencyLimiter()
    monkeypatch.setattr(fx_stream, 'BROKER', broker)
    monkeypatch.setattr(fx_stream, 'IP_LIMITER', limiter)
    response = await fx_stream.stream(pid, SimpleNamespace(
        headers={}, client=SimpleNamespace(host='read-only')))
    try:
        blob = await asyncio.wait_for(response.body_iterator.__anext__(), 5)
        data = json.loads(blob.decode().split('data: ', 1)[1])['data']
        assert Decimal(data['price']) == 1
        assert data['history_ready'] is False and data['history_invalidated'] is True
        # Availability fallback reuses the generator's session after rollback.
        assert len(sessions) == 2
        assert registry._entries[('fx', pid)].state.pool_version == 1
    finally:
        await response.body_iterator.aclose()
    assert broker.subscriber_count(f'fx:{pid}') == 0
