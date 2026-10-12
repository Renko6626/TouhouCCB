"""Authenticated aggregate account and pricing read endpoints."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_async_session
from app.core.users import current_active_user
from app.models.base import User
from app.schemas.trading_context import GroupKey, TradingContext, TradingVersionCheck, TradingPrices
from app.services.credit.trading_context import read_trading_context, read_trading_versions, read_trading_prices, missing_keys
from app.services.trading_prices import now_string

router = APIRouter()
# Task 2 registers authenticated fetch-SSE here; no generator owns a DB session.
stream_router = APIRouter()

def current_group(current_product: Literal['fx', 'lmsr'] | None = None, current_id: int | None = Query(None, gt=0)):
    if (current_product is None) != (current_id is None):
        raise HTTPException(422, 'current_group_requires_product_and_id')
    return GroupKey(product=current_product, id=current_id) if current_product else None

@router.get('/trading-context', response_model=TradingContext)
async def context(user: User = Depends(current_active_user), db: AsyncSession = Depends(get_async_session), group=Depends(current_group)):
    return await read_trading_context(db, user.id, group)

@router.get('/trading-versions', response_model=TradingVersionCheck)
async def versions(user: User = Depends(current_active_user), db: AsyncSession = Depends(get_async_session), group=Depends(current_group)):
    return await read_trading_versions(db, user.id, group)

@router.get('/trading-prices', response_model=TradingPrices)
async def prices(key: list[str] = Query(default=[]), user: User = Depends(current_active_user), db: AsyncSession = Depends(get_async_session), group=Depends(current_group)):
    if len(key) > 200:
        raise HTTPException(422, 'too_many_groups')
    try:
        keys = [GroupKey(product=k.split(':')[0], id=int(k.split(':')[1])) for k in key if len(k.split(':')) == 2]
        if len(keys) != len(key):
            raise ValueError()
    except ValueError:
        raise HTTPException(422, 'invalid_group_key')
    groups = await read_trading_prices(db, user.id, keys, group)
    return TradingPrices(groups=groups, missing_groups=missing_keys(keys, groups), server_time=now_string())

import asyncio
import json
import time
import jwt
from fastapi import Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.core.config import settings
from app.core.database import async_session_maker
from app.core.users import get_current_user
from app.services.account_events import EVENTS
from app.services.trading_prices import REGISTRY, PUBLISHER
from app.services.credit.trading_context import read_dependencies, read_rules, authoritative_version

async def stream_identity(credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer())):
    # Explicit short authentication session: no yield dependency survives the response.
    async with async_session_maker() as db:
        user = await get_current_user(credentials, db)
        user_id = int(user.id)
    # Authentication above already checked signature and expiry. Read the deadline
    # without a second expiry check racing the short database lookup.
    payload = jwt.decode(credentials.credentials, settings.SECRET_KEY, algorithms=['HS256'], options={'verify_exp': False})
    expiry = float(payload.get('exp', time.time() + 3600))
    return user_id, min(expiry, time.time() + 3600)

def encode_frame(event, payload):
    return 'event: ' + event + '\ndata: ' + json.dumps(payload, separators=(',', ':')) + '\n\n'

@stream_router.get('/trading')
async def trading_stream(request: Request, identity=Depends(stream_identity), group=Depends(current_group)):
    user_id, deadline = identity
    subscription = EVENTS.subscribe(user_id)
    try:
        # Subscribe first, then re-read authority: closes context -> subscribe gap.
        async with async_session_maker() as db:
            keys = await read_dependencies(db, user_id, group)
            rules = await read_rules(db)
            version = await authoritative_version(db, user_id)
        PUBLISHER.subscribe(subscription, keys)
        groups = await REGISTRY.refresh_groups(keys)
        sync = dict(economic_version=version, rules=rules.model_dump(), groups=[g.model_dump() for g in groups], missing_groups=[k.model_dump() for k in missing_keys(keys, groups)], server_time=now_string())
    except BaseException:
        PUBLISHER.unsubscribe(subscription)
        EVENTS.unsubscribe(subscription)
        raise

    async def frames():
        last_rule = rules.rule_version
        next_check = time.monotonic() + 15
        pending_frame = None
        try:
            yield encode_frame('sync', sync)
            while time.time() < deadline and not subscription.closed.is_set():
                if await request.is_disconnected():
                    break
                if time.monotonic() >= next_check:
                    # Check the schedule independently of queue availability: a busy
                    # pricing/account stream must still revalidate admission/rules.
                    next_check = time.monotonic() + 15
                    async with async_session_maker() as db:
                        user = await db.get(User, user_id)
                        if user is None or not user.is_active:
                            break
                        refreshed = await read_rules(db)
                        keys_now = await read_dependencies(db, user_id, group)
                    PUBLISHER.subscribe(subscription, keys_now)
                    if refreshed.rule_version != last_rule:
                        last_rule = refreshed.rule_version
                        yield encode_frame('rules', refreshed.model_dump())
                    yield encode_frame('heartbeat', dict(server_time=now_string()))
                    continue
                if pending_frame is None:
                    try:
                        pending_frame = await asyncio.wait_for(subscription.queue.get(), min(max(.01, next_check-time.monotonic()), max(.01, deadline-time.time())))
                    except asyncio.TimeoutError:
                        # Return to the absolute deadline and independent due checks.
                        continue
                # A frame can arrive as the timer becomes due while awaiting it.
                # Retain it in order and revalidate before private delivery.
                if time.monotonic() >= next_check or time.time() >= deadline:
                    continue
                event, payload = pending_frame
                pending_frame = None
                if subscription.closed.is_set():
                    break
                if event == 'account_changed':
                    async with async_session_maker() as db:
                        keys_now = await read_dependencies(db, user_id, group)
                    PUBLISHER.subscribe(subscription, keys_now)
                yield encode_frame(event, payload)
        finally:
            PUBLISHER.unsubscribe(subscription)
            EVENTS.unsubscribe(subscription)
    return StreamingResponse(frames(), media_type='text/event-stream', headers={'Cache-Control':'no-cache', 'X-Accel-Buffering':'no'})
