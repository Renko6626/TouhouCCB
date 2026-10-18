"""FX command ordering, committed publication and bounded-wait behavior."""
import asyncio
from decimal import Decimal as D

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.database import async_session_maker
from app.models.base import User
from app.models.fx import FxPair, FxTrade
from app.services.fx import trading, writer_ops
from app.services.fx.writer import FxWriter
from app.services.fx.writer_ops import SpotTrade
from tests.test_fx_borrow_buy import seed

pytestmark = pytest.mark.asyncio


def buy(uid, pid, key):
    return SpotTrade(pid, uid, 'buy', D('10'), D('0'), key)


async def test_pair_commands_and_reload_wait_for_commit_install_and_hints(monkeypatch):
    uid, pid = await seed()
    writer = FxWriter()
    await writer.start()
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []
    original = trading.execute_trade_in_session

    async def execute(db, *args, **kwargs):
        # The producer's session is already closed; this session belongs to
        # the consumer and remains one transaction through funds + commit.
        observed.append(('execute', args[5]))
        if args[5] == 'first':
            entered.set()
            await release.wait()
        return await original(db, *args, **kwargs)

    def hints(changes, reason):
        observed.append(('published', writer.get_state(pid).snapshot.pool_version))

    monkeypatch.setattr(trading, 'execute_trade_in_session', execute)
    monkeypatch.setattr(writer_ops, 'publish_accounts', hints)
    try:
        first = asyncio.create_task(writer.submit(buy(uid, pid, 'first')))
        await entered.wait()
        second = asyncio.create_task(writer.submit(buy(uid, pid, 'second')))
        reload = asyncio.create_task(writer.reload_state(pid))
        await asyncio.sleep(0)
        assert observed == [('execute', 'first')]
        assert writer.get_state(pid).snapshot.pool_version == 1
        assert not second.done() and not reload.done()
        release.set()
        a, b, state = await asyncio.gather(first, second, reload)
        assert a.id < b.id and state.pool_version == 3
        assert observed == [('execute', 'first'), ('published', 2),
                            ('execute', 'second'), ('published', 3)]
        async with async_session_maker() as db:
            assert (await db.get(User, uid)).cash == D('80')
            assert (await db.get(FxPair, pid)).pool_version == 3
    finally:
        release.set()
        await writer.stop()


async def test_failed_install_recovers_committed_state_before_next_command(monkeypatch):
    uid, pid = await seed()
    writer = FxWriter()
    await writer.start()
    install = writer.install_committed
    failed = False

    def fail_once(snapshot):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError('publication interrupted')
        return install(snapshot)

    monkeypatch.setattr(writer, 'install_committed', fail_once)
    try:
        with pytest.raises(RuntimeError, match='publication interrupted'):
            await writer.submit(buy(uid, pid, 'committed'))
        assert writer.get_state(pid).snapshot.pool_version == 2
        assert not writer.get_state(pid).unavailable
        replay = await writer.submit(buy(uid, pid, 'committed'))
        async with async_session_maker() as db:
            assert (await db.get(User, uid)).cash == D('90')
            assert await db.scalar(select(func.count()).select_from(FxTrade)) == 1
            assert replay.id == (await db.execute(select(FxTrade.id))).scalar_one()
    finally:
        await writer.stop()


async def test_timeout_can_commit_started_command_but_skips_abandoned_queue(monkeypatch):
    uid, pid = await seed()
    writer = FxWriter()
    await writer.start()
    writer.SUBMIT_TIMEOUT = .02
    entered, release = asyncio.Event(), asyncio.Event()
    original = trading.execute_trade_in_session

    async def execute(db, *args, **kwargs):
        entered.set()
        await release.wait()
        return await original(db, *args, **kwargs)

    monkeypatch.setattr(trading, 'execute_trade_in_session', execute)
    try:
        started = asyncio.create_task(writer.submit(buy(uid, pid, 'started')))
        await entered.wait()
        queued = asyncio.create_task(writer.submit(buy(uid, pid, 'abandoned')))
        results = await asyncio.gather(started, queued, return_exceptions=True)
        assert all(isinstance(result, HTTPException) and result.status_code == 503
                   and 'result unknown' in result.detail for result in results)
        writer.SUBMIT_TIMEOUT = 10
        release.set()
        await writer.reload_state(pid)
        async with async_session_maker() as db:
            assert (await db.get(User, uid)).cash == D('90')
            assert (await db.execute(select(FxTrade.idempotency_key))).scalars().all() == ['started']
    finally:
        release.set()
        await writer.stop()


async def test_liquidity_delete_and_pending_trade_share_one_queue(monkeypatch):
    """Deletion commits after funding and rejects every old queued waiter."""
    from app.models.audit import AuditEvent
    from app.services.fx import liquidity
    from app.services.fx.writer_ops import AdminPair, Liquidity
    uid, pid = await seed()
    writer = FxWriter()
    await writer.start()
    entered, release = asyncio.Event(), asyncio.Event()
    original = liquidity._fund_pair_impl
    installed_versions = []

    async def fund(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    def hints(pair):
        installed_versions.append(writer.get_state(pid).snapshot.pool_version)

    monkeypatch.setattr(liquidity, '_fund_pair_impl', fund)
    monkeypatch.setattr(liquidity, '_publish_liquidity', hints)
    try:
        funding = asyncio.create_task(writer.submit(Liquidity(pid, D('3'), D('2'), uid, False)))
        await entered.wait()
        deleting = asyncio.create_task(writer.submit(AdminPair(pid, uid, 'delete', '{}')))
        pending = asyncio.create_task(writer.submit(buy(uid, pid, 'after-delete')))
        reload = asyncio.create_task(writer.reload_state(pid))
        await asyncio.sleep(0)
        assert not deleting.done() and not pending.done() and not reload.done()
        release.set()
        funded, deleted, trade_error, reload_error = await asyncio.gather(
            funding, deleting, pending, reload, return_exceptions=True)
        assert funded.pool_version == 2 and funded.gold_reserve == D('10003')
        assert deleted is None and installed_versions == [2]
        assert all(isinstance(error, HTTPException) and error.status_code == 404
                   for error in (trade_error, reload_error))
        assert writer.get_state(pid).deleted and writer.get_state(pid).unavailable
        with pytest.raises(HTTPException) as rejected:
            await writer.register_pair(pid)
        assert rejected.value.status_code == 404
        async with async_session_maker() as db:
            assert await db.get(FxPair, pid) is None
            assert (await db.get(User, uid)).cash == D('100')
            assert await db.scalar(select(func.count()).select_from(FxTrade)) == 0
            assert set((await db.execute(select(AuditEvent.event_type))).scalars()) == {
                'fx_fund', 'fx_pair_delete'}
    finally:
        release.set()
        await writer.stop()


async def test_queued_cancel_precedes_trigger_without_financial_mutation(monkeypatch):
    from app.models.fx import FxTriggerOrder
    from app.schemas.fx import FxTriggerOrderCreate
    from app.services.fx import liquidity
    from app.services.fx.writer_ops import AttemptOrder, CancelOrder, CreateOrder, Liquidity
    uid, pid = await seed()
    writer = FxWriter()
    await writer.start()
    entered, release = asyncio.Event(), asyncio.Event()
    original = liquidity._fund_pair_impl

    async def fund(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(liquidity, '_fund_pair_impl', fund)
    try:
        req = FxTriggerOrderCreate(pair_id=pid, action='buy', amount=D('10'),
                                   comparison='lte', trigger_price=D('2'))
        order = await writer.submit(CreateOrder(pid, uid, req.model_dump_json()))
        funding = asyncio.create_task(writer.submit(Liquidity(pid, D('1'), D('1'), uid, False)))
        await entered.wait()
        cancelling = asyncio.create_task(writer.submit(CancelOrder(pid, uid, order.id)))
        attempt = asyncio.create_task(writer.submit(AttemptOrder(pid, order.id, D('1'))))
        await asyncio.sleep(0)
        assert not cancelling.done() and not attempt.done()
        release.set()
        _, cancelled, result = await asyncio.gather(funding, cancelling, attempt)
        assert cancelled.status == 'cancelled' and result is None
        async with async_session_maker() as db:
            assert (await db.get(FxTriggerOrder, order.id)).status == 'cancelled'
            assert (await db.get(User, uid)).cash == D('100')
            assert await db.scalar(select(func.count()).select_from(FxTrade)) == 0
    finally:
        release.set()
        await writer.stop()
