from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from app.core.database import async_session_maker
from app.models.base import SiteConfig
from app.services import admin_user_service as admin, site_config
from app.services.credit import risk
from app.services.credit.ownership import OWNERSHIP, EconomicWritesDisabled
from tests.test_credit_risk import _user, _market, _position, NOW, TH, ONE

pytestmark = pytest.mark.asyncio

async def test_locked_risk_refreshes_sell_fee():
    async with async_session_maker() as s:
        s.add(SiteConfig(key='sell_fee_rate', value='0', value_type='decimal'))
        u = await _user(s, 'fee', cash='0', debt='20', accrued=NOW)
        m, outcomes = await _market(s, 'fee', [100,100])
        await _position(s,u,outcomes[0],100)
        deps = await risk.discover_dependencies(s,u.id)
        await s.execute(update(SiteConfig).where(SiteConfig.key=='sell_fee_rate').values(value='0.99'))
        await s.commit()
        decision = await risk.check_cash_spend(s,user=u,deps=deps,spend=Decimal('0'),thresholds=TH,partial_pct=ONE,now=NOW)
        assert not decision.allowed

async def test_changed_interest_rate_rejects_stale_borrow_simulation():
    async with async_session_maker() as s:
        s.add(SiteConfig(key='loan_daily_rate',value='0',value_type='decimal'))
        u=await _user(s,'rate',cash='100',debt='30',accrued=NOW-timedelta(days=10))
        deps=await risk.discover_dependencies(s,u.id)
        await s.execute(update(SiteConfig).where(SiteConfig.key=='loan_daily_rate').values(value='0.1'))
        await s.commit()
        decision=await risk.check_new_risk(s,user=u,deps=deps,post=risk.PostTradeState(cash=Decimal('110'),debt=Decimal('40')),thresholds=TH,partial_pct=ONE,now=NOW)
        assert decision.reason == 'version_conflict'

async def test_readonly_blocks_auth_post_but_preserves_get(monkeypatch):
    from app.main import app
    monkeypatch.setattr(OWNERSHIP,'_reason','read_only')
    monkeypatch.setattr(OWNERSHIP,'_writes_enabled',False)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        response=await client.post('/api/v1/auth/jwt/login',data={})
        assert response.status_code==503
        assert (await client.get('/health')).status_code==200

async def test_readonly_site_config_mutation_is_rejected(monkeypatch):
    async with async_session_maker() as s:
        s.add(SiteConfig(key='final_guard',value='old',value_type='string'))
        await s.commit()
        monkeypatch.setattr(OWNERSHIP,'_reason','read_only')
        monkeypatch.setattr(OWNERSHIP,'_writes_enabled',False)
        with pytest.raises(EconomicWritesDisabled):
            await site_config.set_value(s,'final_guard','new',admin_user_id=None)

@pytest.mark.parametrize('operation',['deduct','batch','loan'])
async def test_admin_rejects_dependency_version_change(monkeypatch,operation):
    async with async_session_maker() as s:
        u=await _user(s,'stale',cash='100',debt='10',accrued=NOW)
        deps=await risk.discover_dependencies(s,u.id)
        u.economic_version+=1
        await s.commit()
        async def stale(*a,**kw): return deps
        monkeypatch.setattr(admin,'_deps_for_cash_write',stale)
        monkeypatch.setattr(admin,'discover_dependencies',stale)
        monkeypatch.setattr(OWNERSHIP,'_writes_enabled',True)
        with pytest.raises(admin.AdminUserError) as exc:
            if operation=='deduct':
                await admin._adjust_cash_unified(s,target_id=u.id,amount=Decimal('-1'),reason='test',admin_id=u.id,thresholds=TH)
            elif operation=='batch':
                await admin._batch_adjust_one(s,user_id=u.id,amount=Decimal('-1'),reason='test',admin_id=u.id,thresholds=TH)
            else:
                await admin._force_loan_unified(s,target_id=u.id,amount=Decimal('1'),reason='test',admin_id=u.id,rate=Decimal('0'),thresholds=TH)
        assert exc.value.status==409

async def test_admin_new_principal_has_no_historical_interest(monkeypatch):
    from app.services.credit.thresholds import derive_thresholds
    async with async_session_maker() as s:
        s.add(SiteConfig(key='loan_daily_rate',value='0.1',value_type='decimal'))
        u=await _user(s,'interest',cash='30',debt='10',accrued=NOW-timedelta(days=1))
        monkeypatch.setattr(OWNERSHIP,'_writes_enabled',True)
        monkeypatch.setattr(admin.loan_service,'_compat_now',lambda u: NOW)
        result=await admin._force_loan_unified(s,target_id=u.id,amount=Decimal('40'),reason='test',admin_id=u.id,rate=Decimal('0.1'),thresholds=derive_thresholds(Decimal('4'),Decimal('0.1')))
        assert result['debt']==51.0
        assert result['cash']==70.0

@pytest.mark.parametrize("key", ["loan_daily_rate", "sell_fee_rate"])
async def test_unified_rate_change_requires_maintenance(monkeypatch, key):
    from app.services.credit import flags
    async with async_session_maker() as s:
        s.add(SiteConfig(key=key,value='0.01',value_type='decimal'))
        await s.commit()
        monkeypatch.setattr(flags,'get_flags',lambda: type('Flags',(),{'unified_credit_enabled':True})())
        monkeypatch.setattr(OWNERSHIP,'_writes_enabled',True)
        with pytest.raises(site_config.SiteConfigError):
            await site_config.set_value(s,key,'0.02',admin_user_id=None)
        row=await site_config.set_value(s,key,'0.010',admin_user_id=None)
        assert Decimal(row.value)==Decimal('0.01')


@pytest.mark.parametrize("key", ["loan_daily_rate", "sell_fee_rate"])
async def test_legacy_config_rate_change_remains_available(monkeypatch, key):
    from app.services.credit import flags
    async with async_session_maker() as s:
        s.add(SiteConfig(key=key, value='0.01', value_type='decimal'))
        await s.commit()
        monkeypatch.setattr(flags, 'get_flags', lambda: type('Flags', (), {'unified_credit_enabled': False})())
        monkeypatch.setattr(OWNERSHIP, '_writes_enabled', True)
        row = await site_config.set_value(s, key, '0.02', admin_user_id=None)
        assert Decimal(row.value) == Decimal('0.02')
