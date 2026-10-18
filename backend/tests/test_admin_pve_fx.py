"""Core FX account lifecycle, independent of the management HTTP layer."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import func, select

from app.models.audit import AuditEvent
from app.models.base import User
from app.models.bot import BotProfile
from app.models.fx import FxTrade, FxWallet, FxShortPosition
from app.schemas.pve_fx import FxPveGenerateRequest, FxPvePatchRequest
from app.services.credit import ownership
from app.services.pve import service as pve_service
from app.services.pve_fx import accounts
from app.services.pve_fx.presets import SHORT_EPISODE_PROFILES
from app.services.pve.service import BotOpError
from tests.fx_test_helpers import add_pair, fx_db


@pytest.fixture(autouse=True)
def use_test_owner(fx_db, monkeypatch):
    monkeypatch.setattr(accounts, 'OWNERSHIP', ownership.OWNERSHIP)
    monkeypatch.setattr(pve_service, 'OWNERSHIP', ownership.OWNERSHIP)


def request(pair_id, **overrides):
    return FxPveGenerateRequest(pair_id=pair_id, **{
        'items': [{'template': 'inventory', 'count': 1}],
        'initial_cash': '1234', 'initial_foreign': '600', **overrides,
    })


@pytest.mark.asyncio
async def test_custom_allocation_and_zero_cash_values_do_not_mutate_pool(fx_db):
    pair, treasury = await add_pair(fx_db, gold='10000', foreign='5000')
    item = (await accounts.generate_fx_bots(fx_db, request(pair.id), 1))[0]
    assert (item.cash, item.wallet, item.inventory_baseline_foreign, item.status) == (
        Decimal('1234'), Decimal('600'), Decimal('600'), 'paused')
    wallet = (await fx_db.execute(select(FxWallet).where(FxWallet.user_id == item.user_id))).scalars().one()
    assert wallet.cost_basis == Decimal('1200')
    assert (pair.gold_reserve, pair.foreign_reserve, treasury.gold_balance, treasury.foreign_balance) == (
        Decimal('10000'), Decimal('5000'), Decimal('10000'), Decimal('5000'))
    assert (await fx_db.execute(select(func.count()).select_from(FxTrade))).scalar_one() == 0
    allocation = (await fx_db.execute(select(AuditEvent).where(
        AuditEvent.event_type == 'pve_fx_initial_allocation'))).scalars().one()
    assert allocation.user_id == item.user_id
    assert allocation.payload['foreign_amount'] == '600'
    zero_cash = (await accounts.generate_fx_bots(fx_db, request(pair.id, initial_cash='0'), 1))[0]
    assert zero_cash.cash == 0
    assert zero_cash.mtm == 1200
    assert Decimal('1000') < zero_cash.liquidation_cash < Decimal('1200')
    with pytest.raises(BotOpError, match='外汇持仓'):
        await accounts.retire_fx_bot(fx_db, item.profile_id, 1)


@pytest.mark.asyncio
async def test_invalid_allocation_rejected_before_any_account(fx_db):
    pair, _ = await add_pair(fx_db, gold='1000000', foreign='1')
    with pytest.raises(BotOpError, match='成本超过'):
        await accounts.generate_fx_bots(fx_db, request(pair.id, initial_foreign='10000'), 1)
    for amount in ('NaN', '-1', '1.0000001', '10000000000'):
        with pytest.raises(ValidationError):
            request(pair.id, initial_foreign=amount)
    with pytest.raises(ValidationError):
        request(pair.id, items=[{'template': 'unknown', 'count': 1}])
    pair.archived = True
    with pytest.raises(BotOpError, match='未归档'):
        await accounts.generate_fx_bots(fx_db, request(pair.id), 1)
    assert (await fx_db.execute(select(func.count()).select_from(BotProfile))).scalar_one() == 0
    assert (await fx_db.execute(select(func.count()).select_from(User))).scalar_one() == 0


@pytest.mark.asyncio
async def test_strategy_patches_baseline_and_frozen_episode_profiles(fx_db):
    pair, _ = await add_pair(fx_db)
    items = await accounts.generate_fx_bots(fx_db, request(pair.id, items=[{'template':'episode', 'count':len(SHORT_EPISODE_PROFILES) + 1}]), 1)
    assert [i.params['trend'] for i in items] == [p['trend'] for p in SHORT_EPISODE_PROFILES] + [SHORT_EPISODE_PROFILES[0]['trend']]
    item = items[0]
    with pytest.raises(BotOpError, match='只读'):
        await accounts.patch_fx_bot(fx_db, item.profile_id, FxPvePatchRequest(params={'inventory_baseline_foreign':'600'}), 1)
    profile = await fx_db.get(BotProfile, item.profile_id)
    profile.strategy_state = {'episode': {'remaining':'1'}}
    with pytest.raises(BotOpError, match='轮次'):
        await accounts.patch_fx_bot(fx_db, item.profile_id, FxPvePatchRequest(params={'trend':1}), 1)
    with pytest.raises(BotOpError, match='轮次'):
        await accounts.patch_fx_bot(fx_db, item.profile_id, FxPvePatchRequest(template='random'), 1)
    await accounts.patch_fx_bot(fx_db, item.profile_id, FxPvePatchRequest(status='active'), 1)
    await accounts.patch_fx_bot(fx_db, item.profile_id, FxPvePatchRequest(status='paused'), 1)
    profile.strategy_state = {}
    updated = await accounts.patch_fx_bot(fx_db, item.profile_id, FxPvePatchRequest(template='inventory'), 1)
    assert updated.params['inventory_baseline_foreign'] == '600'
    assert updated.wallet == 600
    await accounts.patch_fx_bot(fx_db, item.profile_id, FxPvePatchRequest(status='active'), 1)
    updated = await accounts.patch_fx_bot(fx_db, item.profile_id, FxPvePatchRequest(params={'inventory_sensitivity':42}), 1)
    assert updated.status == 'active' and updated.params['inventory_sensitivity'] == 42


@pytest.mark.asyncio
async def test_funding_statuses_and_retirement_guards(fx_db):
    pair, _ = await add_pair(fx_db)
    item = (await accounts.generate_fx_bots(fx_db, request(pair.id, initial_foreign='0'), 1))[0]
    funded = await accounts.fund_fx_bot(fx_db, item.profile_id, Decimal('10'), 1)
    assert (funded.cash, funded.status) == (Decimal('1244'), 'paused')
    profile = await fx_db.get(BotProfile, item.profile_id)
    profile.status = 'dead'
    assert (await accounts.fund_fx_bot(fx_db, item.profile_id, Decimal('10'), 1)).status == 'active'
    user = await fx_db.get(User, item.user_id)
    user.debt = Decimal('1')
    with pytest.raises(BotOpError, match='债务'):
        await accounts.retire_fx_bot(fx_db, item.profile_id, 1)
    user.debt = Decimal('0')
    profile.status = 'retired'
    with pytest.raises(BotOpError, match='退休'):
        await accounts.fund_fx_bot(fx_db, item.profile_id, Decimal('10'), 1)
    with pytest.raises(BotOpError, match='退休'):
        await accounts.patch_fx_bot(fx_db, item.profile_id, FxPvePatchRequest(status='active'), 1)


@pytest.mark.asyncio
async def test_empty_account_deletion_and_traded_account_retirement(fx_db, monkeypatch):
    async def delete(row):
        fx_db.sync_session.delete(row)
    monkeypatch.setattr(fx_db, 'delete', delete, raising=False)
    pair, _ = await add_pair(fx_db)
    item = (await accounts.generate_fx_bots(fx_db, request(pair.id, initial_foreign='0'), 1))[0]
    assert not (await fx_db.execute(select(AuditEvent).where(
        AuditEvent.event_type == 'pve_fx_initial_allocation'))).scalars().all()
    result = await accounts.retire_fx_bot(fx_db, item.profile_id, 1)
    await fx_db.flush()
    assert result['mode'] == 'deleted'
    assert await fx_db.get(User, item.user_id) is None
    assert await fx_db.get(BotProfile, item.profile_id) is None

    item = (await accounts.generate_fx_bots(fx_db, request(pair.id, initial_foreign='0'), 1))[0]
    # Seed a historical trade row to exercise the retained-identity branch.
    fx_db.add(FxTrade(user_id=item.user_id, pair_id=pair.id, side='sell', input_amount=Decimal('1'),
        output_amount=Decimal('1'), pre_gold_reserve=Decimal('101'), pre_foreign_reserve=Decimal('99'),
        post_gold_reserve=Decimal('100'), post_foreign_reserve=Decimal('100'), post_price=Decimal('1')))
    await fx_db.flush()
    result = await accounts.retire_fx_bot(fx_db, item.profile_id, 1)
    user = await fx_db.get(User, item.user_id)
    profile = await fx_db.get(BotProfile, item.profile_id)
    assert result['mode'] == 'retired'
    assert result['recovered_cash'] == 1234
    assert user.cash == 0 and user.is_active is False
    assert profile.status == 'retired'
    assert (await fx_db.execute(select(FxTrade).where(FxTrade.user_id == user.id))).scalars().one()


@pytest.mark.asyncio
async def test_retirement_rejects_independent_foreign_debt(fx_db):
    pair, _ = await add_pair(fx_db)
    item = (await accounts.generate_fx_bots(fx_db, request(pair.id, initial_foreign='0'), 1))[0]
    fx_db.add(FxShortPosition(user_id=item.user_id, pair_id=pair.id, principal_foreign=Decimal('1'),
        interest_last_accrued_at=datetime.now(timezone.utc)))
    await fx_db.flush()
    with pytest.raises(BotOpError, match='外币债务'):
        await accounts.retire_fx_bot(fx_db, item.profile_id, 1)

# HTTP coverage focuses on route admission and composition; finance behavior is covered above.
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from app.api.v1 import admin_pve_fx
from app.core import database
from app.core.users import current_superuser, current_active_user
from app.models.fx import FxPair, FxTreasury

P = '/api/v1/admin/pve/fx'


@pytest_asyncio.fixture
async def fx_api_runtime(fx_db, monkeypatch):
    from app.services import trading_prices
    from app.services.fx import writer as fx_writer
    from app.services.pve_fx import executor

    # Match the lifespan without replacing the existing isolated owner/factory.
    registry = trading_prices.PricingRegistry(database.async_session_maker)
    registry.configure(memory_products=frozenset({'fx'}))
    monkeypatch.setattr(trading_prices, 'OWNERSHIP', ownership.OWNERSHIP)
    monkeypatch.setattr(trading_prices, 'REGISTRY', registry)
    monkeypatch.setattr(executor, 'async_session_maker', database.async_session_maker)
    writer = fx_writer.FxWriter()
    monkeypatch.setattr(fx_writer, 'FX_WRITER', writer)
    try:
        await writer.start()
        yield
    finally:
        await writer.stop()


async def api_seed():
    async with database.async_session_maker() as db:
        admin = User(username='fx-admin', is_superuser=True, is_active=True)
        pair = FxPair(currency_code='API', currency_name='API', status='trading',
                      gold_reserve=Decimal('10000'), foreign_reserve=Decimal('10000'))
        db.add(admin)
        db.add(pair)
        await db.flush()
        db.add(FxTreasury(pair_id=pair.id, gold_balance=Decimal('10000'), foreign_balance=Decimal('10000')))
        await db.commit()
        pair_id = pair.id
    # The test seeds directly instead of using admin pair creation.
    from app.services.fx.writer import FX_WRITER
    await FX_WRITER.register_pair(pair_id)
    return admin, pair_id

def management_app(admin=None):
    app = FastAPI()
    app.include_router(admin_pve_fx.router, prefix=P)
    if admin is not None:
        app.dependency_overrides[current_superuser] = lambda: admin
    return app

@pytest.mark.asyncio
async def test_fx_management_requires_superuser():
    app = management_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        for path in ['/overview', '/config', '/bots', '/bots/1/log']:
            assert (await client.get(P+path)).status_code == 401
        for method,path,body in [('put','/config',{'enabled':False}),
                                ('post','/bots/generate',{}),('post','/bots/batch',{}),
                                ('patch','/bots/1',{}),('post','/bots/1/fund',{}),
                                ('post','/bots/1/liquidate',{})]:
            assert (await client.request(method,P+path,json=body)).status_code == 401
        assert (await client.delete(P+'/bots/1')).status_code == 401
        app.dependency_overrides[current_active_user] = lambda: User(username='ordinary', is_superuser=False)
        assert (await client.get(P+'/overview')).status_code == 403
        assert (await client.post(P+'/bots/batch', json={'profile_ids':[1],'patch':{'status':'active'}})).status_code == 403

@pytest.mark.asyncio
async def test_fx_api_batch_real_params_and_atomic_validation(fx_api_runtime):
    admin,pair_id = await api_seed()
    async with AsyncClient(transport=ASGITransport(app=management_app(admin)), base_url='http://test') as client:
        result = await client.post(P+'/bots/generate',json={
            'pair_id':pair_id,'items':[{'template':'random','count':2}],
            'initial_cash':'123','initial_foreign':'2'})
        assert result.status_code == 200, result.text
        items=result.json()['created']
        ids=[b['profile_id'] for b in items]
        assert all(b['status']=='paused' for b in items)
        result=await client.post(P+'/bots/batch',json={'profile_ids':ids,'patch':{'params':{'order_sigma_foreign':37}}})
        assert result.status_code == 200, result.text
        assert [b['params']['order_sigma_foreign'] for b in result.json()['updated']]==[37,37]
        # A later invalid/foreign profile must roll back changes to earlier selections.
        result=await client.post(P+'/bots/batch',json={'profile_ids':[ids[0],99999],'patch':{'status':'active'}})
        assert result.status_code==404
        result=await client.get(P+'/bots')
        assert [b['status'] for b in result.json()]==['paused','paused']
        result=await client.post(P+'/bots/batch',json={'profile_ids':ids,'patch':{'params':{'inventory_baseline_foreign':'99'}}})
        assert result.status_code==400
        assert (await client.get(P+'/overview')).json()['preset']['items'][-1]['template']=='episode'
        async with database.async_session_maker() as db:
            second = await db.get(BotProfile, ids[1])
            second.status = 'retired'
            await db.commit()
        result = await client.post(P+'/bots/batch', json={'profile_ids':ids,'patch':{'params':{'order_sigma_foreign':99}}})
        assert result.status_code == 409
        persisted = (await client.get(P+'/bots')).json()
        assert persisted[0]['params']['order_sigma_foreign'] == 37

@pytest.mark.asyncio
async def test_fx_api_paused_liquidation_then_retirement(fx_api_runtime):
    admin,pair_id=await api_seed()
    async with AsyncClient(transport=ASGITransport(app=management_app(admin)), base_url='http://test') as client:
        result=await client.post(P+'/bots/generate',json={
            'pair_id':pair_id,'items':[{'template':'inventory','count':1}],
            'initial_cash':'123','initial_foreign':'2'})
        item=result.json()['created'][0]
        pid=item['profile_id']
        assert (await client.delete(f'{P}/bots/{pid}')).status_code==409
        result=await client.post(f'{P}/bots/{pid}/liquidate',json={'min_out':'0'})
        assert result.status_code==200,result.text
        bot=result.json()['bot']
        assert bot['status']=='paused' and Decimal(bot['wallet'])==0
        assert Decimal(bot['cash'])>Decimal('123')
        assert not bot['strategy_state'].get('episode')
        result=await client.delete(f'{P}/bots/{pid}')
        assert result.status_code==200,result.text
        assert result.json()['mode']=='retired'
        async with database.async_session_maker() as db:
            profile=await db.get(BotProfile,pid)
            user=await db.get(User,item['user_id'])
            assert profile.status=='retired' and not user.is_active and user.cash==0


@pytest.mark.asyncio
@pytest.mark.parametrize('preset,window,quantity,noise', [
    ('warmup', 3000, '342.0', 68.4),
    ('activity', 375, '180', 108),
])
async def test_generation_applies_selected_preset(fx_db, preset, window, quantity, noise):
    pair, _ = await add_pair(fx_db)
    items = await accounts.generate_fx_bots(fx_db, request(pair.id, preset=preset, items=[
        {'template': 'random', 'count': 1}, {'template': 'episode', 'count': 7},
    ]), 1)
    assert items[0].params['order_sigma_foreign'] == pytest.approx(noise)
    assert all(i.params['trend_window_seconds'] == window for i in items[1:])
    assert all(Decimal(i.params['entry_quantity']) == Decimal(quantity) for i in items[1:])
    # Three mean-reversion accounts followed by four trend accounts.
    assert [i.params['reversion'] > .4 for i in items[1:]] == [True]*3 + [False]*4
