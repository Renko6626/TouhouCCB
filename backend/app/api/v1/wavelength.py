"""Wavelength server notifications and audience API use separate credentials."""
import asyncio
import hmac
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.core.database import async_session_maker, get_async_session
from app.core.users import current_active_user
from app.models.base import User
from app.models.wavelength import WavelengthRound
from app.schemas.wavelength import (MyPrediction, PredictionInput, RoundKey,
    RoundPublic, RoundResult, RoundStart, RoundSummary)
from app.services import wavelength as svc, wavelength_events, wavelength_config
from app.services.account_events import encode_frame


async def require_enabled():
    # Own a short read session; an SSE connection must not retain a DB connection.
    async with async_session_maker() as db:
        config = await wavelength_config.read_config(db)
    if not config['enabled']:
        raise HTTPException(404, 'Not Found')
    return config


def require_source(request: Request, config=Depends(require_enabled)):
    if not config['secret_hash']:
        raise HTTPException(503, 'Wavelength 联动密钥未配置')
    scheme, _, credential = request.headers.get('authorization', '').partition(' ')
    if scheme.lower() != 'bearer' or not hmac.compare_digest(wavelength_config.secret_digest(credential), config['secret_hash']):
        raise HTTPException(401, '联动密钥无效')


router = APIRouter(dependencies=[Depends(require_enabled)])
source = APIRouter(dependencies=[Depends(require_source)])


@source.post('/rounds/start', response_model=RoundPublic)
async def start(req: RoundStart, db: AsyncSession = Depends(get_async_session)):
    return await svc.start_round(db, req)


@source.post('/rounds/{round_key}/close', response_model=RoundPublic)
async def close(round_key: RoundKey, db: AsyncSession = Depends(get_async_session)):
    return await svc.close_round(db, round_key)


@source.post('/rounds/{round_key}/result', response_model=RoundSummary)
async def result(round_key: RoundKey, req: RoundResult, db: AsyncSession = Depends(get_async_session)):
    return await svc.finish_round(db, round_key, target=req.target_position)


@source.post('/rounds/{round_key}/cancel', response_model=RoundSummary)
async def cancel(round_key: RoundKey, db: AsyncSession = Depends(get_async_session)):
    return await svc.finish_round(db, round_key, cancel=True)


router.include_router(source)


@router.get('/current', response_model=RoundPublic | None)
async def current(db: AsyncSession = Depends(get_async_session)):
    round = (await db.execute(select(WavelengthRound).order_by(WavelengthRound.id.desc()).limit(1))).scalar_one_or_none()
    return svc.public_round(round) if round else None


@router.get('/rounds/{round_key}/me', response_model=MyPrediction)
async def mine(round_key: RoundKey, user: User = Depends(current_active_user),
               db: AsyncSession = Depends(get_async_session)):
    return await svc.get_mine(db, round_key, user.id)


@router.put('/rounds/{round_key}/prediction', response_model=MyPrediction)
async def prediction(round_key: RoundKey, req: PredictionInput,
                     user: User = Depends(current_active_user), db: AsyncSession = Depends(get_async_session)):
    return await svc.submit_prediction(db, round_key, user.id, req.position)


@router.get('/stream')
async def stream(request: Request):
    try:
        queue = wavelength_events.subscribe()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None

    async def frames():
        deadline = time.monotonic() + 3600
        try:
            # Subscribe before telling the client to fetch current state.
            yield encode_frame('sync', {'refresh_current': True})
            while time.monotonic() < deadline:
                if await request.is_disconnected():
                    break
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    async with async_session_maker() as db:
                        if not (await wavelength_config.read_config(db))['enabled']:
                            break
                    yield b': keepalive\n\n'
        finally:
            wavelength_events.unsubscribe(queue)

    return StreamingResponse(frames(), media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
