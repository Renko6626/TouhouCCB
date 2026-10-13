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
