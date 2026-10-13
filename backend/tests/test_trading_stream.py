"""Real authenticated ASGI HTTP stream, without buffered infinite-client transport."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
import jwt
import pytest
from sqlalchemy import update
from app.main import app
from app.core.config import settings
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.models.fx import FxPair, FxWallet
from app.schemas.trading_context import GroupKey
from app.services.account_events import EVENTS, enqueue_account_change
from app.services.trading_prices import PUBLISHER, REGISTRY

async def request_stream(token, on_frame=None, query=b''):
    stopped = asyncio.Event()
    messages = []
    first = True
    async def receive():
        nonlocal first
        if first:
            first = False
            return {'type':'http.request', 'body':b'', 'more_body':False}
        await stopped.wait()
        return {'type':'http.disconnect'}
    async def send(message):
        messages.append(message)
        body = message.get('body', b'')
        if body.startswith(b'event:'):
            frame = body.decode()
            event, data = frame.splitlines()[:2]
            if on_frame:
                await on_frame(event[7:], json.loads(data[6:]), stopped)
            else:
                stopped.set()
    scope = dict(type='http', asgi={'version':'3.0'}, http_version='1.1', method='GET', scheme='http', path='/api/v1/stream/trading', raw_path=b'/api/v1/stream/trading', query_string=query, root_path='', headers=[(b'authorization', ('Bearer '+token).encode())], client=('127.0.0.1',1), server=('test',80))
    await asyncio.wait_for(app(scope, receive, send), 5)
    return messages

async def seed():
    async with async_session_maker() as db:
        user, other = User(username='stream-user'), User(username='stream-other')
        pairs = [FxPair(currency_code=str(i), currency_name=str(i)) for i in range(5)]
        db.add_all([user,other,*pairs]); await db.flush()
        db.add_all([FxWallet(user_id=user.id,pair_id=p.id,foreign_amount=1) for p in pairs]); await db.commit()
        return user.id, other.id, [p.id for p in pairs]

@pytest.mark.asyncio
async def test_authenticated_five_groups_private_and_cleanup():
    uid, other, ids = await seed()
    seen = []
    async def on_frame(event, payload, stopped):
        seen.append((event,payload))
        if event == 'sync':
            enqueue_account_change(other, 77, 'foreign')
            enqueue_account_change(uid, 2, 'own')
        elif event == 'account_changed':
            stopped.set()
    messages = await request_stream(create_access_token(uid), on_frame)
    assert next(m for m in messages if m['type']=='http.response.start')['status'] == 200
    assert {g['id'] for g in seen[0][1]['groups']} == set(ids)
    assert [(p['reason'],p['economic_version']) for e,p in seen if e=='account_changed'] == [('own','2')]
    assert uid not in EVENTS._subscribers and not PUBLISHER.subscriptions

@pytest.mark.asyncio
async def test_expired_credentials_and_slow_queue_cleanup():
    uid, _, _ = await seed()
    token = jwt.encode({'sub':str(uid),'exp':datetime.now(timezone.utc)-timedelta(seconds=1),'type':'access'},settings.SECRET_KEY,algorithm='HS256')
    messages = await request_stream(token)
    assert messages[0]['status'] == 401 and uid not in EVENTS._subscribers
    async def overload(event,payload,stopped):
        if event == 'sync':
            for version in range(65):
                enqueue_account_change(uid, version, 'overflow')
    await request_stream(create_access_token(uid),overload)
    assert uid not in EVENTS._subscribers and not PUBLISHER.subscriptions

@pytest.mark.asyncio
async def test_first_sync_recovers_commit_before_subscription():
    uid, _, _ = await seed()
    # No subscriber existed for this durable version change.
    async with async_session_maker() as db:
        await db.execute(update(User).where(User.id==uid).values(economic_version=9));await db.commit()
    messages = await request_stream(create_access_token(uid))
    body = next(m['body'] for m in messages if m.get('body',b'').startswith(b'event: sync'))
    assert json.loads(body.decode().splitlines()[1][6:])['economic_version'] == '9'

@pytest.mark.asyncio
async def test_shared_pricing_frame_uses_committed_complete_inputs():
    uid, _, ids = await seed()
    frames = []
    async def on_frame(event,payload,stopped):
        frames.append((event,payload))
        if event == 'sync':
            async with async_session_maker() as db:
                await db.execute(update(FxPair).where(FxPair.id==ids[0]).values(gold_reserve=123,foreign_reserve=456,sell_fee_rate='0.12345678'));await db.commit()
            PUBLISHER.wake.set()
        if event == 'pricing':
            if any(g['id']==ids[0] and g['gold_reserve']=='123.000000' for g in payload['groups']):
                stopped.set()
    PUBLISHER.start()
    try:
        await request_stream(create_access_token(uid),on_frame)
    finally:
        await PUBLISHER.stop()
    group = next(g for e,p in frames if e=='pricing' for g in p['groups'] if g['id']==ids[0] and g['gold_reserve']=='123.000000')
    assert group['foreign_reserve']=='456.000000' and group['sell_fee_rate']=='0.12345678'
    fresh = await REGISTRY.refresh_groups([GroupKey(product='fx',id=ids[0])])
    assert group['stamp'] == fresh[0].stamp.model_dump()

@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['rules', 'ban'])
async def test_due_rechecks_run_with_continuously_available_frames(monkeypatch, change):
    from types import SimpleNamespace
    import time as real_time
    from sqlalchemy import select
    from app.api.v1 import user_trading
    from app.models.base import SiteConfig
    from app.services import site_config

    uid, _, _ = await seed()
    clock = [0.0]
    # Advance only this route's elapsed clock; event loop scheduling stays real.
    monkeypatch.setattr(user_trading, 'time', SimpleNamespace(time=real_time.time, monotonic=lambda: clock[0]))
    seen = []
    async def on_frame(event, payload, stopped):
        seen.append((event, payload))
        if event == 'sync':
            async with async_session_maker() as db:
                if change == 'ban':
                    await db.execute(update(User).where(User.id==uid).values(is_active=False))
                else:
                    value = 'false' if payload['rules']['loan_enabled'] else 'true'
                    row = (await db.execute(select(SiteConfig).where(SiteConfig.key=='loan_enabled'))).scalar_one_or_none()
                    if row is None:
                        db.add(SiteConfig(key='loan_enabled', value=value, value_type='bool'))
                    else:
                        row.value = value
                await db.commit()
            site_config.clear_cache()
            for version in range(32):
                enqueue_account_change(uid, version, 'continuous')
            clock[0] = 16.0
        elif event == 'heartbeat':
            stopped.set()
    await request_stream(create_access_token(uid), on_frame)
    if change == 'ban':
        assert [event for event, _ in seen] == ['sync']
    else:
        changed_rules = [payload for event, payload in seen if event == 'rules']
        assert changed_rules and any(event == 'heartbeat' for event, _ in seen)
        assert changed_rules[0]['loan_enabled'] != seen[0][1]['rules']['loan_enabled']
        # The old timeout-only loop drained all 32 available frames first.
        assert sum(event == 'account_changed' for event, _ in seen) < 32
    assert uid not in EVENTS._subscribers and not PUBLISHER.subscriptions


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['succeeded', 'failed'])
async def test_fx_order_result_private_stream(status):
    from app.services.account_events import enqueue_order_result
    from app.schemas.fx import FxOrderResult
    uid, other, ids = await seed()
    seen = []
    async def on_frame(event, payload, stopped):
        seen.append((event, payload))
        if event == 'sync':
            result = FxOrderResult(order_id=1, pair_id=ids[0], currency_code='0',
                action='buy', status=status, occurred_at=datetime.now(timezone.utc))
            enqueue_order_result(other, result.model_copy(update={'order_id':2}))
            enqueue_order_result(uid, result)
        elif event == 'fx_order_result':
            stopped.set()
    await request_stream(create_access_token(uid), on_frame)
    assert [(p['order_id'],p['status']) for e,p in seen if e == 'fx_order_result'] == [(1,status)]
    assert not any(e == 'account_changed' for e,p in seen)
