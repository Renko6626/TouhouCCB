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
