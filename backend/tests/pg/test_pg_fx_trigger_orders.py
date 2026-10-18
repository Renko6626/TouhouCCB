"""Only PG row-lock guarantees missing from the storage tests."""
import asyncio
import os

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.models.base import User
from app.models.fx import FxExecution, FxPair, FxTrade, FxTriggerOrder, FxWallet
from app.schemas.fx import FxTriggerOrderCreate
from app.services.fx import orders, net_trading, trading
from tests.fx_test_helpers import committed_fx_registry, assert_committed_fx
from tests.pg.test_pg_fx_short_concurrency import seed, writer

pytestmark = [pytest.mark.pg, pytest.mark.asyncio]


@pytest.fixture(scope='session', autouse=True)
def exact_disposable_database():
    # Runs before the function-scoped pg_engine's schema reset.
    url = make_url(os.environ.get('TEST_PG_DATABASE_URL', ''))
    old_orders = (url.host == '127.0.0.1' and url.port == 55547
                  and url.database == 'thccb_fx_orders_test')
    committed_reads = (url.query.get('host') == '/tmp/sunyunbo/fx-pool-incident-20261017/socket'
                       and url.query.get('port') == '55489'
                       and url.database == 'fx_market_memory_test')
    if url.drivername != 'postgresql+asyncpg' or not (old_orders or committed_reads):
        raise pytest.UsageError('Requires an approved disposable FX test database')


@pytest_asyncio.fixture(autouse=True)
async def order_sessions(pg_sessionmaker, monkeypatch, writer):
    import app.core.database
    monkeypatch.setattr(app.core.database, 'async_session_maker', pg_sessionmaker)
    from app.services import trading_prices
    from app.services.credit import ownership
    monkeypatch.setattr(trading_prices, 'OWNERSHIP', ownership.OWNERSHIP)


def request(pid):
    return FxTriggerOrderCreate(pair_id=pid, action='buy', amount='10',
                                comparison='lte', trigger_price='1')


async def test_concurrent_creation_holds_account_limit(pg_sessionmaker):
    uid, pid = await seed(pg_sessionmaker)
    async with pg_sessionmaker() as db:
        extra_pairs = [FxPair(currency_code=f'SLOT{i}', currency_name=f'Slot {i}',
                              status='trading') for i in range(4)]
        db.add_all(extra_pairs)
        await db.flush()
        pair_ids = [pid, *(pair.id for pair in extra_pairs)]
        await db.commit()
    # Separate Pair locks must still serialize on this account's User lock.
    async def create(pair_id):
        async with pg_sessionmaker() as db:
            try:
                return await orders.create_order(db, uid, request(pair_id))
            except HTTPException as exc:
                await db.rollback()
                assert exc.status_code == 409
                return None
    created = await asyncio.wait_for(asyncio.gather(*(create(pair_id) for pair_id in pair_ids)), 10)
    assert sum(result is not None for result in created) == 3
    async with pg_sessionmaker() as db:
        assert (await db.execute(select(func.count()).select_from(FxTriggerOrder))).scalar_one() == 3
        assert (await db.get(User, uid)).cash == 1000


@pytest.mark.parametrize('winner', ['trigger', 'cancel'])
async def test_cancel_trigger_row_lock_has_one_financial_outcome(pg_sessionmaker, monkeypatch, winner, committed_fx_registry):
    uid, pid = await seed(pg_sessionmaker)
    async with pg_sessionmaker() as db:
        order = await orders.create_order(db, uid, request(pid))
    entered, release = asyncio.Event(), asyncio.Event()
    async def cancel():
        async with pg_sessionmaker() as db:
            try:
                return await orders.cancel_order(db, uid, order.id)
            except HTTPException as exc:
                await db.rollback()
                assert exc.status_code == 409
                return None
    if winner == 'trigger':
        original = net_trading.execute_fx_instruction_in_session
        async def held_execution(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(net_trading, 'execute_fx_instruction_in_session', held_execution)
        first = asyncio.create_task(orders.attempt_order(order.id, 1))
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(cancel())
    else:
        original = orders.lock_user
        async def held_user(*args, **kwargs):
            user = await original(*args, **kwargs)
            if not entered.is_set():
                entered.set()
                await release.wait()
            return user
        monkeypatch.setattr(orders, 'lock_user', held_user)
        first = asyncio.create_task(cancel())
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(orders.attempt_order(order.id, 1))
    # Let the losing operation reach its real database lock.
    await asyncio.sleep(.05)
    assert not second.done()
    release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), 10)
    assert results[0].status == ('succeeded' if winner == 'trigger' else 'cancelled')
    assert results[1] is None
    async with pg_sessionmaker() as db:
        final = await db.get(FxTriggerOrder, order.id)
        user = await db.get(User, uid)
        receipts = (await db.execute(select(FxExecution))).scalars().all()
        trades = (await db.execute(select(FxTrade))).scalars().all()
        wallets = (await db.execute(select(FxWallet))).scalars().all()
        if winner == 'trigger':
            assert final.status == 'succeeded' and final.execution_id == receipts[0].id
            assert len(receipts) == len(trades) == len(wallets) == 1
            assert user.cash == 990 and wallets[0].foreign_amount == trades[0].output_amount
            await assert_committed_fx(committed_fx_registry, db, pid)
        else:
            assert final.status == 'cancelled' and final.execution_id is None
            assert user.cash == 1000 and receipts == trades == wallets == []


async def test_success_rescan_and_failed_order_preserve_later_projection(pg_sessionmaker, committed_fx_registry):
    uid, pid = await seed(pg_sessionmaker)
    async with pg_sessionmaker() as db:
        succeeded = await orders.create_order(db, uid, request(pid))
        rejected = await orders.create_order(db, uid, FxTriggerOrderCreate(pair_id=pid,
            action='buy', amount='999999', comparison='lte', trigger_price='100'))
    assert (await orders.attempt_order(succeeded.id, 1)).status == 'succeeded'
    async with pg_sessionmaker() as db:
        await assert_committed_fx(committed_fx_registry, db, pid)
        await trading.execute_trade(db, uid, pid, 'buy', 1, 0, 'later')
    latest = committed_fx_registry.peek_fx(pid)
    assert latest is not None
    assert await orders.attempt_order(succeeded.id, 1) is None
    assert (await orders.attempt_order(rejected.id, 1)).status == 'failed'
    assert committed_fx_registry.peek_fx(pid) == latest
