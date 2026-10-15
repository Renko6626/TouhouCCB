"""Core FX account lifecycle, independent of the management HTTP layer."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
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
    items = await accounts.generate_fx_bots(fx_db, request(pair.id, items=[{'template':'episode', 'count':6}]), 1)
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
