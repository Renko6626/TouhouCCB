"""Private order lifecycle and account slot limit, using real storage."""
from decimal import Decimal as D

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.database import async_session_maker
from app.schemas.fx import FxTriggerOrderCreate
from app.services.fx.orders import create_order, list_orders, cancel_order
from tests.test_fx_short_trading import _seed

pytestmark = pytest.mark.asyncio


def request(pid, **changes):
    return FxTriggerOrderCreate(**dict(pair_id=pid, action='buy', amount=D('10'),
        comparison='lte', trigger_price=D('1'), **changes))


async def test_slots_cancel_and_private_paginated_history():
    uid, pid = await _seed(cash='0')
    from app.models.base import User
    from app.models.fx import FxPair
    async with async_session_maker() as db:
        other_user = User(username='other-order-user')
        other_pair = FxPair(currency_code='OTHER', currency_name='Other', status='trading')
        db.add_all([other_user, other_pair])
        await db.flush()
        other, other_pid = other_user.id, other_pair.id
        await db.commit()
    async with async_session_maker() as db:
        created = [await create_order(db, uid, request(pair_id))
                   for pair_id in (pid, other_pid, pid)]
        with pytest.raises(HTTPException) as exc:
            await create_order(db, uid, request(pid))
        assert exc.value.status_code == 409
        await db.rollback()
        assert await list_orders(db, other) == []
        with pytest.raises(HTTPException) as exc:
            await cancel_order(db, other, created[0].id)
        assert exc.value.status_code == 404
        await db.rollback()
        cancelled = await cancel_order(db, uid, created[0].id)
        assert cancelled.status == 'cancelled' and cancelled.finished_at is not None
        assert await cancel_order(db, uid, cancelled.id) == cancelled
        fourth = await create_order(db, uid, request(pid))
        first_page = await list_orders(db, uid, limit=2)
        assert [r.id for r in first_page] == [fourth.id, created[2].id]
        second_page = await list_orders(db, uid, before_id=first_page[-1].id, limit=2)
        assert [r.id for r in second_page] == [created[1].id, cancelled.id]
        assert [r.id for r in await list_orders(db, uid, status='cancelled')] == [cancelled.id]


async def test_finished_order_cannot_cancel_and_hidden_pair_cannot_create():
    from app.models.fx import FxPair, FxTriggerOrder
    uid, pid = await _seed()
    async with async_session_maker() as db:
        order = await create_order(db, uid, request(pid))
        row = await db.get(FxTriggerOrder, order.id)
        row.status = 'failed'
        await db.commit()
        with pytest.raises(HTTPException) as exc:
            await cancel_order(db, uid, order.id)
        assert exc.value.status_code == 409
        await db.rollback()
        pair = await db.get(FxPair, pid)
        pair.archived = True
        await db.commit()
        with pytest.raises(HTTPException) as exc:
            await create_order(db, uid, request(pid))
        assert exc.value.status_code == 404
        await db.rollback()
        assert (await list_orders(db, uid))[0].status == 'failed'


async def test_order_rejects_invalid_financing_and_precision():
    for values in ({'borrow_limit': D('1')}, {'trigger_price': D('0')},
                   {'trigger_price': D('1.0000001')},
                   {'action': 'sell', 'auto_borrow': True},
                   {'action': 'close_long', 'amount': None, 'close_percent': D('100.1')}):
        params = dict(pair_id=1, action='buy', amount=D('10'), comparison='gte', trigger_price=D('1'))
        params.update(values)
        with pytest.raises(ValidationError):
            FxTriggerOrderCreate(**params)


async def test_historical_order_requires_archive_instead_of_pair_delete():
    from app.api.v1.admin_fx import delete_pair
    from app.models.base import User
    from app.models.fx import FxPair
    uid, pid = await _seed()
    async with async_session_maker() as db:
        order = await create_order(db, uid, request(pid))
        await cancel_order(db, uid, order.id)
        admin = await db.get(User, uid)
        with pytest.raises(HTTPException) as exc:
            await delete_pair(pid, admin, db)
        assert exc.value.status_code == 409 and 'archive' in exc.value.detail
        await db.rollback()
        assert await db.get(FxPair, pid) is not None


async def _order(uid, pid, **changes):
    params = dict(pair_id=pid, action='buy', amount=D('10'), comparison='lte', trigger_price=D('1'))
    params.update(changes)
    async with async_session_maker() as db:
        return await create_order(db, uid, FxTriggerOrderCreate(**params))


@pytest.mark.parametrize('action', ['buy', 'sell'])
async def test_trigger_condition_cross_zero_and_repeat(action):
    from sqlalchemy import select
    from app.models.base import User
    from app.models.fx import FxExecution, FxShortPosition, FxTrade, FxWallet
    from app.services.fx.orders import attempt_order, result_for_order
    uid, pid = await _seed(cash='1000', wallet_foreign='5' if action == 'sell' else None,
        short={'principal':'5', 'restricted':'10'} if action == 'buy' else None)
    order = await _order(uid, pid, action=action, amount=D('20'),
        comparison='gte' if action == 'sell' else 'lte')
    assert await attempt_order(order.id, D('0.5') if action == 'sell' else D('2')) is None
    result = await attempt_order(order.id, D('1'))
    assert result.status == 'succeeded' and result.observed_trigger_price == 1
    assert await attempt_order(order.id, D('1')) is None
    async with async_session_maker() as db:
        receipts = (await db.execute(select(FxExecution))).scalars().all()
        trades = (await db.execute(select(FxTrade).order_by(FxTrade.id))).scalars().all()
        assert len(receipts) == 1 and len(trades) == 2
        assert [t.purpose for t in trades] == (['short_cover', 'spot'] if action == 'buy' else ['spot', 'short_open'])
        position = (await db.execute(select(FxShortPosition))).scalar_one()
        wallet = (await db.execute(select(FxWallet))).scalar_one()
        user = await db.get(User, uid)
        if action == 'buy':
            assert position.principal_foreign + position.interest_foreign == 0
            assert wallet.foreign_amount == trades[-1].output_amount and user.cash == 980
        else:
            assert wallet.foreign_amount == 0 and position.principal_foreign == 15
            assert user.cash == 1000 + sum(t.output_amount for t in trades)
        notification = await result_for_order(db, result)
        assert notification.order_id == order.id and notification.input_amount == 20
        assert notification.borrow_amount == 0


@pytest.mark.parametrize('failure', ['cash', 'authorization', 'paused', 'disabled'])
async def test_business_failure_rolls_back_and_releases_slot(failure):
    from sqlalchemy import func, select
    from app.models.base import User, SiteConfig
    from app.models.fx import FxExecution, FxPair, FxTrade, FxTriggerOrder, FxWallet
    from app.services.fx.orders import attempt_order, result_for_order
    from app.services import site_config
    uid, pid = await _seed(cash='0' if failure in ('cash', 'authorization') else '1000')
    order = await _order(uid, pid, auto_borrow=failure == 'authorization',
        borrow_limit=D('9') if failure == 'authorization' else D('0'))
    await _order(uid, pid)
    await _order(uid, pid)
    async with async_session_maker() as db:
        if failure == 'paused':
            pair = await db.get(FxPair, pid)
            pair.status, pair.reduce_only = 'paused', True
        if failure == 'disabled':
            config = (await db.execute(select(SiteConfig).where(SiteConfig.key == 'fx_enabled'))).scalar_one()
            config.value = 'false'
        await db.commit()
    site_config.clear_cache()
    result = await attempt_order(order.id, D('1'))
    assert result.status == 'failed' and result.failure_reason and result.execution_id is None
    async with async_session_maker() as db:
        assert (await db.get(User, uid)).cash == (0 if failure in ('cash','authorization') else 1000)
        assert (await db.get(User, uid)).debt == 0
        for model in (FxExecution, FxTrade, FxWallet):
            assert (await db.execute(select(func.count()).select_from(model))).scalar_one() == 0
        assert (await db.get(FxTriggerOrder, order.id)).status == 'failed'
        assert (await result_for_order(db, result)).failure_reason == result.failure_reason
        assert (await create_order(db, uid, request(pid))).status == 'waiting'


async def test_full_short_close_includes_interest():
    from sqlalchemy import select
    from app.models.fx import FxShortPosition, FxTrade
    from app.services.fx.orders import attempt_order, result_for_order
    uid, pid = await _seed(short={'principal':'9', 'interest':'1', 'restricted':'20'})
    order = await _order(uid, pid, action='close_short', amount=None, close_percent=D('100'))
    result = await attempt_order(order.id, D('1'))
    assert result.status == 'succeeded' and result.trade_id and result.execution_id is None
    async with async_session_maker() as db:
        position = (await db.execute(select(FxShortPosition))).scalar_one()
        trade = await db.get(FxTrade, result.trade_id)
        assert position.principal_foreign == position.interest_foreign == position.restricted_gold == 0
        assert trade.output_amount == 10
        notification = await result_for_order(db, result)
        assert notification.output_amount == 10 and notification.input_amount == trade.input_amount


async def test_commit_failure_rolls_back_cross_zero_and_propagates(monkeypatch):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models.base import User
    from app.models.fx import FxExecution, FxPair, FxShortPosition, FxTrade, FxTriggerOrder, FxWallet
    from app.services.fx.orders import attempt_order
    uid, pid = await _seed(cash='20', short={'principal':'10', 'restricted':'20'})
    order = await _order(uid, pid, amount=D('30'), auto_borrow=True, borrow_limit=D('10'))
    async def reject_commit(db):
        await db.rollback()
        raise RuntimeError('test commit failed before commit')
    with monkeypatch.context() as scoped:
        scoped.setattr(AsyncSession, 'commit', reject_commit)
        with pytest.raises(RuntimeError, match='test commit failed'):
            await attempt_order(order.id, D('1'))
    async with async_session_maker() as db:
        assert (await db.get(FxTriggerOrder, order.id)).status == 'waiting'
        user, pair = await db.get(User, uid), await db.get(FxPair, pid)
        assert user.cash == 20 and user.debt == 0
        assert pair.gold_reserve == pair.foreign_reserve == 1000
        position = (await db.execute(select(FxShortPosition))).scalar_one()
        assert position.principal_foreign == 10 and position.restricted_gold == 20
        for model in (FxExecution, FxTrade, FxWallet):
            assert (await db.execute(select(func.count()).select_from(model))).scalar_one() == 0


async def test_scan_batch_observations_and_permanent_failure():
    from app.models.fx import FxPair
    from app.services.fx.order_scheduler import run_order_scan_once
    uid, pid = await _seed()
    async with async_session_maker() as db:
        first = await create_order(db, uid, request(pid))
        second = await create_order(db, uid, request(pid))
        waiting = await create_order(db, uid, FxTriggerOrderCreate(pair_id=pid,
            action='buy', amount=D('10'), comparison='gte', trigger_price=D('999')))
    assert await run_order_scan_once() == dict(checked=3, triggered=2, succeeded=2, failed=0)
    async with async_session_maker() as db:
        history = {o.id:o for o in await list_orders(db, uid)}
        assert history[first.id].observed_trigger_price == history[second.id].observed_trigger_price == D('1')
        assert history[waiting.id].status == 'waiting'
        pair = await db.get(FxPair, pid)
        pair.status = 'closed'
        await db.commit()
    assert await run_order_scan_once() == dict(checked=1, triggered=0, succeeded=0, failed=1)


async def test_scan_paused_trigger_fails():
    from app.models.fx import FxPair
    from app.services.fx.order_scheduler import run_order_scan_once
    uid, pid = await _seed()
    async with async_session_maker() as db:
        await create_order(db, uid, request(pid))
        pair = await db.get(FxPair, pid)
        pair.status = 'paused'
        await db.commit()
    assert await run_order_scan_once() == dict(checked=1, triggered=1, succeeded=0, failed=1)


async def test_scheduler_stop_waits_for_current_scan(monkeypatch):
    import asyncio
    from app.services.fx import order_scheduler
    entered, finish = asyncio.Event(), asyncio.Event()
    async def scan():
        entered.set()
        await finish.wait()
    monkeypatch.setattr(order_scheduler, 'run_order_scan_once', scan)
    await order_scheduler.start_scheduler()
    tick = asyncio.create_task(order_scheduler._tick_safe())
    await entered.wait()
    await order_scheduler.reschedule(15)
    stopping = asyncio.create_task(order_scheduler.stop_scheduler())
    await asyncio.sleep(0)  # Yield only; no wall-clock delay.
    assert not stopping.done() and not tick.done()
    finish.set()
    await stopping
    await tick
    assert order_scheduler._scheduler is None
