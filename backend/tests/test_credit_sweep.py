import asyncio
from decimal import Decimal as D
import pytest
import pytest_asyncio
from sqlalchemy import select
from app.core.database import async_session_maker
from app.models.base import User, SiteConfig
from app.models.credit import LiquidationRun
from app.services.credit.flags import CreditFlags, set_flags
from app.services.credit.ownership import OWNERSHIP
from app.services import liquidation_sweep, site_config
from app.services.credit import sweep

@pytest_asyncio.fixture(autouse=True)
async def lifecycle():
    await OWNERSHIP.acquire()
    set_flags(CreditFlags(credit_leverage=D('20'),credit_maintenance_ratio=D('.04')))
    async with async_session_maker() as s:
        for k,v in [('liquidation_enabled','true'),('loan_daily_rate','0'),('liquidation_partial_pct','.1')]:
            old=(await s.execute(select(SiteConfig).where(SiteConfig.key==k))).scalar_one_or_none()
            if old:old.value=v
            else:s.add(SiteConfig(key=k,value=v,value_type='str'))
        await s.commit()
    site_config.clear_cache()
    yield
    set_flags(CreditFlags())

@pytest.mark.asyncio
async def test_all_pages_workers_bounded_and_overlap_skipped(monkeypatch):
    async with async_session_maker() as s:
        s.add_all([User(username=f'page{i}',casdoor_id=f'p{i}',cash=D('0'),debt=D('1')) for i in range(47)])
        await s.commit()
    running=0;peak=0;seen=[];entered=asyncio.Event();release=asyncio.Event()
    async def execute(uid,**kwargs):
        nonlocal running,peak
        running+=1;peak=max(peak,running);seen.append(uid);entered.set()
        await release.wait()
        await asyncio.sleep(0)
        running-=1
        return 'triggered'
    monkeypatch.setattr(sweep,'execute_user',execute)
    task=asyncio.create_task(liquidation_sweep.run_liquidation_sweep_once())
    await entered.wait()
    assert await liquidation_sweep.run_liquidation_sweep_once('admin_manual')=={'skipped':'sweep_in_progress'}
    release.set();result=await task
    assert result['scanned_count']==47 and result['triggered_count']==47
    assert peak==3 and len(set(seen))==47 and seen==sorted(seen)

@pytest.mark.asyncio
async def test_existing_run_above_maintenance_and_zero_debt_still_visited(monkeypatch):
    from datetime import datetime,timezone
    async with async_session_maker() as s:
        a=User(username='a',cash=D('105'),debt=D('100'));b=User(username='b',cash=D('0'),debt=D('0'))
        s.add_all([a,b]);await s.flush()
        s.add_all([LiquidationRun(user_id=u.id,trigger_source='scheduler',started_at=datetime.now(timezone.utc)) for u in [a,b]])
        await s.commit();ids={a.id,b.id}
    seen=[]
    async def execute(uid,**kwargs):seen.append(uid);return 'recovered'
    monkeypatch.setattr(sweep,'execute_user',execute)
    result=await liquidation_sweep.run_liquidation_sweep_once()
    assert set(seen)==ids and result['recovered_count']==2

@pytest.mark.asyncio
async def test_disabled_run_now_and_no_extra_triggers(monkeypatch):
    async with async_session_maker() as s:
        cfg=(await s.execute(select(SiteConfig).where(SiteConfig.key=='liquidation_enabled'))).scalar_one();cfg.value='false';await s.commit()
    site_config.clear_cache()
    async def forbidden(*args,**kwargs):raise AssertionError('disabled execution')
    monkeypatch.setattr(sweep,'execute_user',forbidden)
    assert await liquidation_sweep.run_liquidation_sweep_once('admin_manual')=={'skipped':'disabled'}
    # No subscriber / event callback is registered by this module: its sole entry is the wrapper.
    assert not hasattr(sweep,'on_trade') and not hasattr(sweep,'mark_dirty')


@pytest.mark.asyncio
async def test_blocked_and_deadlock_metrics_are_real(monkeypatch):
    from sqlalchemy.exc import DBAPIError
    async with async_session_maker() as s:
        s.add_all([User(username=f'metric{i}',cash=D('0'),debt=D('1')) for i in range(3)])
        await s.commit()
        ids=list((await s.execute(select(User.id).order_by(User.id))).scalars())
    async def execute(uid,**kwargs):
        if uid==ids[0]:return 'blocked'
        if uid==ids[1]:raise DBAPIError('test',{},Exception('deadlock detected'))
        return 'triggered'
    monkeypatch.setattr(sweep,'execute_user',execute)
    result=await liquidation_sweep.run_liquidation_sweep_once()
    assert result['blocked_count']==1 and result['skipped_count']==1
    assert result['deadlocks']==1 and result['errors']==1
    assert result['monetary_action_count']==1
    assert result['execution_duration_ms']>=result['max_user_execution_ms']>=0
