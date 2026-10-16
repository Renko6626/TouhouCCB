"""Superuser-only FX bot management; lifecycle services own the business rules."""
from dataclasses import asdict
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_async_session, managed_transaction
from app.core.users import current_superuser
from app.models.base import User
from app.models.bot import BotProfile
from app.models.fx import FxPair
from app.schemas.pve_fx import FxPveConfig, FxPairBotConfig, FxPveGenerateRequest, FxPvePatchRequest, FxPveFundRequest
from app.services.pve.service import BotOpError
from app.services.pve_fx import accounts
from app.services.pve_fx.config import load_fx_config, save_fx_config
from app.services.pve_fx.engine import get_fx_pve_engine
from app.services.pve_fx.executor import liquidate_fx_bot
from app.services.pve_fx.presets import FROZEN_PRESET, PRESETS
from app.services.pve_fx.registry import strategy_metadata
from app.services.fx.amm import marginal_price

router = APIRouter(dependencies=[Depends(current_superuser)])

class BatchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    profile_ids: list[int] = Field(min_length=1)
    patch: FxPvePatchRequest

class LiquidateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    min_out: Decimal = Field(ge=0, decimal_places=6)

async def require_profile(db, profile_id):
    return await accounts._profile(db, profile_id)

def error(exc):
    return HTTPException(exc.status_code if isinstance(exc, BotOpError) else 400,
                         detail=exc.detail if isinstance(exc, BotOpError) else str(exc))

@router.get('/overview')
async def overview(db: AsyncSession = Depends(get_async_session)):
    pairs = (await db.execute(select(FxPair).order_by(FxPair.id))).scalars().all()
    return {'templates': strategy_metadata(), 'preset': FROZEN_PRESET, 'presets': list(PRESETS.values()),
            'pair_config_schema': FxPairBotConfig.model_json_schema(),
            'engine': get_fx_pve_engine().snapshot(),
            'pairs': [{'id': p.id, 'code': p.currency_code, 'name': p.currency_name,
                       'price': str(marginal_price(p.gold_reserve, p.foreign_reserve)),
                       'status': p.status, 'archived': p.archived} for p in pairs]}

@router.get('/config')
async def config(db: AsyncSession = Depends(get_async_session)):
    return await load_fx_config(db)

@router.put('/config')
async def put_config(payload: FxPveConfig, db: AsyncSession = Depends(get_async_session), admin: User = Depends(current_superuser)):
    async with managed_transaction(db):
        for pair_id in payload.pair_configs:
            if await db.get(FxPair, pair_id) is None:
                raise HTTPException(400, detail=f'交易对 {pair_id} 不存在')
        await save_fx_config(db, payload, admin.id)
    return payload

@router.get('/bots')
async def bots(pair_id: int | None = None, template: str | None = None, status: str | None = None, db: AsyncSession = Depends(get_async_session)):
    stmt = select(BotProfile).where(BotProfile.market_kind == 'fx').order_by(BotProfile.id)
    for column, value in ((BotProfile.fx_pair_id, pair_id), (BotProfile.template, template), (BotProfile.status, status)):
        if value is not None:
            stmt = stmt.where(column == value)
    return [await accounts.fx_bot_item(db, p) for p in (await db.execute(stmt)).scalars().all()]

@router.post('/bots/generate')
async def generate(payload: FxPveGenerateRequest, db: AsyncSession = Depends(get_async_session), admin: User = Depends(current_superuser)):
    try:
        async with managed_transaction(db):
            created = await accounts.generate_fx_bots(db, payload, admin.id)
        return {'created': created}
    except (BotOpError, ValueError) as exc:
        raise error(exc)

@router.post('/bots/batch')
async def batch(payload: BatchRequest, db: AsyncSession = Depends(get_async_session), admin: User = Depends(current_superuser)):
    try:
        async with managed_transaction(db):
            profiles = [await require_profile(db, i) for i in dict.fromkeys(payload.profile_ids)]
            if len({(p.fx_pair_id, p.template) for p in profiles}) != 1:
                raise HTTPException(400, detail='批量操作须选择同一交易对、同一策略的账户')
            items = [await accounts.patch_fx_bot(db, p.id, payload.patch, admin.id) for p in profiles]
        return {'updated': items}
    except (BotOpError, ValueError) as exc:
        raise error(exc)

@router.patch('/bots/{profile_id}')
async def patch(profile_id: int, payload: FxPvePatchRequest, db: AsyncSession = Depends(get_async_session), admin: User = Depends(current_superuser)):
    try:
        async with managed_transaction(db):
            return await accounts.patch_fx_bot(db, profile_id, payload, admin.id)
    except (BotOpError, ValueError) as exc:
        raise error(exc)

@router.post('/bots/{profile_id}/fund')
async def fund(profile_id: int, payload: FxPveFundRequest, db: AsyncSession = Depends(get_async_session), admin: User = Depends(current_superuser)):
    try:
        async with managed_transaction(db):
            return await accounts.fund_fx_bot(db, profile_id, payload.amount, admin.id, payload.reason)
    except (BotOpError, ValueError) as exc:
        raise error(exc)

@router.post('/bots/{profile_id}/liquidate')
async def liquidate(profile_id: int, payload: LiquidateRequest, db: AsyncSession = Depends(get_async_session), admin: User = Depends(current_superuser)):
    try:
        await require_profile(db, profile_id)
        operator_id = admin.id
        # Executor commits independently; release the request's read transaction first.
        await db.rollback()
        result = await liquidate_fx_bot(profile_id, payload.min_out, operator_id)
        if result.rejection:
            raise HTTPException(409, detail=result.rejection)
        db.expire_all()
        profile = await require_profile(db, profile_id)
        return {'execution': asdict(result), 'bot': await accounts.fx_bot_item(db, profile)}
    except (BotOpError, ValueError) as exc:
        raise error(exc)

@router.delete('/bots/{profile_id}')
async def retire(profile_id: int, db: AsyncSession = Depends(get_async_session), admin: User = Depends(current_superuser)):
    try:
        async with managed_transaction(db):
            profile = await require_profile(db, profile_id)
            if profile.status != 'paused':
                raise HTTPException(409, detail='请先暂停机器人再退休')
            return await accounts.retire_fx_bot(db, profile_id, admin.id)
    except (BotOpError, ValueError) as exc:
        raise error(exc)

@router.get('/bots/{profile_id}/log')
async def log(profile_id: int, db: AsyncSession = Depends(get_async_session)):
    try:
        await require_profile(db, profile_id)
        return {'profile_id': profile_id, 'log': get_fx_pve_engine().get_log(profile_id)}
    except BotOpError as exc:
        raise error(exc)
