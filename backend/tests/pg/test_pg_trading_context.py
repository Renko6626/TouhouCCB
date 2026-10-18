import asyncio
from decimal import Decimal
import pytest
from sqlalchemy import select, update
from app.models.base import User
from app.models.fx import FxPair,FxWallet
from app.schemas.trading_context import GroupKey
from app.services.trading_prices import PricingRegistry
from app.services.credit.trading_context import read_trading_context
pytestmark=[pytest.mark.pg,pytest.mark.asyncio]

async def test_atomic_commit_crosses_read(pg_sessionmaker):
    async with pg_sessionmaker() as db:
        user=User(username='atomic',cash=Decimal('100')); pair=FxPair(currency_code='P',currency_name='P')
        db.add_all([user,pair]);await db.flush()
        db.add(FxWallet(user_id=user.id,pair_id=pair.id,foreign_amount=Decimal('1')));await db.commit()
        registry=PricingRegistry(pg_sessionmaker); original=registry.refresh_groups; committed=False
        async def crossing(keys):
            nonlocal committed
            if not committed:
                committed=True
                async with pg_sessionmaker() as writer:
                    await writer.execute(update(User).where(User.id==user.id).values(cash=Decimal('90'),economic_version=1))
                    await writer.execute(update(FxWallet).where(FxWallet.user_id==user.id).values(foreign_amount=Decimal('11')))
                    await writer.execute(update(FxPair).where(FxPair.id==pair.id).values(gold_reserve=Decimal('2'),pool_version=2));await writer.commit()
            return await original(keys)
        registry.refresh_groups=crossing
        result=await read_trading_context(db,user.id,registry=registry,session_factory=pg_sessionmaker)
        assert user.economic_version==0
        assert result.account.economic_version=='1' and result.account.cash=='90.000000'
        assert result.account.fx_wallets[0].foreign_amount=='11.000000'
        assert result.groups[0].gold_reserve=='2.000000'
        assert result.minimum_group_stamps[0].stamp==result.groups[0].stamp

async def test_serial_refresh_old_response(pg_sessionmaker):
    async with pg_sessionmaker() as db:
        pair=FxPair(currency_code='S',currency_name='S');db.add(pair);await db.commit();key=GroupKey(product='fx',id=pair.id)
    registry=PricingRegistry(pg_sessionmaker); original=registry._snapshot
    entered,release=asyncio.Event(),asyncio.Event(); first=True
    async def paused(db,key):
        nonlocal first
        result=await original(db,key)
        if first: first=False;entered.set();await release.wait()
        return result
    registry._snapshot=paused
    old_task=asyncio.create_task(registry.refresh_groups([key]));await entered.wait()
    async with pg_sessionmaker() as db:
        await db.execute(update(FxPair).where(FxPair.id==key.id).values(gold_reserve=Decimal('2')));await db.commit()
    new_task=asyncio.create_task(registry.refresh_groups([key]));release.set()
    old,new=await asyncio.gather(old_task,new_task)
    assert old[0].gold_reserve=='1.000000' and new[0].gold_reserve=='2.000000'
    assert int(new[0].stamp.revision)>int(old[0].stamp.revision)
    assert (await registry.refresh_groups([key]))[0].stamp==new[0].stamp

@pytest.mark.parametrize('route', ['trading-context', 'trading-versions', 'trading-prices'])
async def test_authenticated_reads_release_connection_before_business_read(pg_engine, pg_sessionmaker, monkeypatch, route):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.api.v1 import user_trading
    from app.core import database
    from app.core.users import create_access_token
    from app.services.credit import trading_context

    async with pg_sessionmaker() as db:
        user = User(username='pooled-reader')
        pair = FxPair(currency_code='POOL', currency_name='Pool')
        db.add_all([user, pair])
        await db.commit()
        user_id, pair_id = user.id, pair.id

    # One connection exposes nested checkout without timing many requests.
    engine = create_async_engine(pg_engine.url, pool_size=1, max_overflow=0, pool_timeout=.2)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, 'async_session_maker', factory)
    monkeypatch.setattr(user_trading, 'async_session_maker', factory)
    monkeypatch.setattr(trading_context, 'REGISTRY', PricingRegistry(factory))
    app = FastAPI()
    app.include_router(user_trading.router)
    params = {'current_product': 'fx', 'current_id': pair_id}
    if route == 'trading-prices':
        params['key'] = f'fx:{pair_id}'
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get(f'/{route}', params=params,
                headers={'Authorization': f'Bearer {create_access_token(user_id)}'})
        assert response.status_code == 200, response.text
        body = response.json()
        if route == 'trading-context':
            assert body['account']['user_id'] == user_id
        groups = body['group_stamps'] if route == 'trading-versions' else body['groups']
        assert [(group['product'], group['id']) for group in groups] == [('fx', pair_id)]
    finally:
        await engine.dispose()


from tests.pg.test_pg_fx_short_concurrency import seed, writer
from tests.fx_test_helpers import assert_committed_fx


@pytest.fixture
def market_registry(pg_sessionmaker, monkeypatch):
    from app.services import trading_prices
    from app.services.credit import ownership
    registry = PricingRegistry(pg_sessionmaker)
    registry.configure(memory_products=frozenset({'fx'}))
    monkeypatch.setattr(trading_prices, 'OWNERSHIP', ownership.OWNERSHIP)
    monkeypatch.setattr(trading_prices, 'REGISTRY', registry)
    return registry


async def test_context_returns_fresh_account_before_snapshot_install(pg_sessionmaker, market_registry, monkeypatch):
    from contextlib import asynccontextmanager
    from app.core import database
    from app.services.fx import trading
    from app.services.fx.writer import FX_WRITER
    uid, pid = await seed(pg_sessionmaker)
    await FX_WRITER.start()
    old = await market_registry.read_fx(pid)
    committed, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def execution_session():
        async with pg_sessionmaker() as db:
            original = db.commit
            async def held_commit():
                await original()
                committed.set()
                await release.wait()
            monkeypatch.setattr(db, 'commit', held_commit)
            yield db

    monkeypatch.setattr(database, 'async_session_maker', execution_session)

    async def write():
        async with pg_sessionmaker() as db:
            return await trading.execute_trade(db, uid, pid, 'buy', Decimal('10'), Decimal('0'), 'context-gap')

    async def context():
        async with pg_sessionmaker() as db:
            return await read_trading_context(db, uid, registry=market_registry,
                                               session_factory=pg_sessionmaker)

    writing = asyncio.create_task(write())
    await asyncio.wait_for(committed.wait(), 5)
    try:
        snapshot = await asyncio.wait_for(context(), 5)
        assert market_registry.peek_fx(pid) is old
        assert snapshot.account.cash == '990.000000'
        assert snapshot.account.economic_version == '1'
        assert snapshot.account.fx_wallets[0].foreign_amount != '0.000000'
        assert snapshot.groups[0].gold_reserve == format(old.gold_reserve, 'f')
        assert snapshot.groups[0].pool_version == '1'
        assert snapshot.minimum_group_stamps[0].stamp == snapshot.groups[0].stamp
    finally:
        release.set()
    await asyncio.wait_for(writing, 5)
    published = await context()
    async with pg_sessionmaker() as db:
        pair = await db.get(FxPair, pid)
        assert published.groups[0].gold_reserve == format(pair.gold_reserve, 'f')
        assert published.groups[0].pool_version == str(pair.pool_version)
        wallet = (await db.execute(select(FxWallet).where(FxWallet.user_id == uid))).scalar_one()
        assert snapshot.account.fx_wallets[0].foreign_amount == format(wallet.foreign_amount, 'f')
        await assert_committed_fx(market_registry, db, pid)


async def test_old_cold_read_cannot_overwrite_real_trade(pg_sessionmaker, market_registry):
    from app.services.fx import trading
    from app.services.fx.writer import FX_WRITER
    uid, pid = await seed(pg_sessionmaker)
    market_registry.invalidate(GroupKey(product="fx", id=pid))
    queried, release = asyncio.Event(), asyncio.Event()
    snapshot = market_registry._snapshot
    async def old_read(db, key):
        state = await snapshot(db, key)
        queried.set()
        await release.wait()
        return state
    market_registry._snapshot = old_read
    cold = asyncio.create_task(market_registry.read_fx(pid))
    await asyncio.wait_for(queried.wait(), 5)
    async def trade():
        async with pg_sessionmaker() as db:
            await trading.execute_trade(db, uid, pid, 'buy', Decimal('10'), Decimal('0'), 'after-cold')
    try:
        await FX_WRITER.start()
        await asyncio.wait_for(trade(), 5)
        assert not cold.done()
        assert market_registry.peek_fx(pid).pool_version == 2
    finally:
        release.set()
    assert (await asyncio.wait_for(cold, 5)).pool_version == 2
    assert (await market_registry.read_fx(pid)).pool_version == 2
    async with pg_sessionmaker() as db:
        await assert_committed_fx(market_registry, db, pid)


async def test_admin_fee_snapshot_uses_postgres_stored_precision(pg_sessionmaker, market_registry):
    from app.api.v1.admin_fx import create_pair, update_pair, PairCreate, PairPatch
    from app.services.fx.writer import FX_WRITER
    await FX_WRITER.start()
    async with pg_sessionmaker() as db:
        admin = User(username='precision-admin', is_superuser=True)
        db.add(admin)
        await db.commit()
        created = await create_pair(PairCreate(currency_code='PRECISE', currency_name='Precision',
            buy_fee_rate=Decimal('0.123456785'), gold_reserve=Decimal('100.123456'),
            foreign_reserve=Decimal('200.654321')), admin, db)
        pid = created.id
        await assert_committed_fx(market_registry, db, pid)
        stored = (await db.execute(select(FxPair.buy_fee_rate).where(FxPair.id == pid))).scalar_one()
        assert market_registry.peek_fx(pid).buy_fee_rate == stored
        assert format(market_registry.peek_fx(pid).buy_fee_rate, 'f') == format(stored, 'f')
        await update_pair(pid, PairPatch(sell_fee_rate=Decimal('0.123456785')), admin, db)
        await assert_committed_fx(market_registry, db, pid)
        stored = (await db.execute(select(FxPair.sell_fee_rate).where(FxPair.id == pid))).scalar_one()
        assert market_registry.peek_fx(pid).sell_fee_rate == stored
        assert format(market_registry.peek_fx(pid).sell_fee_rate, 'f') == format(stored, 'f')
