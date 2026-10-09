"""Real PG admission serialization and gate-wait resource checks (local disposable DB)."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from app.api.v1.loan import borrow
from app.models.base import User, SiteConfig, Market, MarketStatus, Outcome, Position
from app.models.fx import FxPair, FxTreasury, FxWallet
from app.schemas.loan import BorrowRequest
from app.services import site_config
from app.services.credit import flags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import OWNERSHIP
from app.services.fx.trading import execute_trade

pytestmark = [pytest.mark.pg, pytest.mark.asyncio]

@pytest.fixture(autouse=True)
def unified(monkeypatch):
    flags.set_flags(flags.CreditFlags( credit_leverage=D('4'), credit_maintenance_ratio=D('.1')))
    monkeypatch.setattr(OWNERSHIP, '_writes_enabled', True)
    site_config.clear_cache()
    yield
    flags.clear_flags()
    site_config.clear_cache()

async def seed(factory):
    async with factory() as s:
        u = User(username='mixed', cash=D('0'), debt=D('0'), tos_accepted_at=datetime.now(timezone.utc))
        v = User(username='independent', cash=D('100'), tos_accepted_at=datetime.now(timezone.utc))
        m = Market(title='collateral', status=MarketStatus.TRADING, liquidity_b=1000)
        pairs = [FxPair(currency_code=code,currency_name=code,status='trading',gold_reserve=D('10000'),foreign_reserve=D('50000')) for code in ('MORA','LMD')]
        s.add_all([u,v,m,*pairs]); await s.flush()
        a=Outcome(market_id=m.id,label='a',total_shares=D('100'))
        b=Outcome(market_id=m.id,label='b',total_shares=D('0'))
        s.add_all([a,b]);await s.flush()
        s.add(Position(user_id=u.id,outcome_id=a.id,amount=D('100')))
        s.add(FxWallet(user_id=u.id,pair_id=pairs[0].id,foreign_amount=D('1000')))
        s.add_all([FxTreasury(pair_id=p.id) for p in pairs])
        for key,value in [('loan_enabled','true'),('fx_enabled','true'),('loan_daily_rate','0'),('loan_leverage_k','3')]:
            s.add(SiteConfig(key=key,value=value,value_type='string'))
        await s.commit()
        return u.id,v.id,m.id,pairs[0].id,pairs[1].id

async def test_mixed_collateral_cannot_fund_two_concurrent_loans(pg_sessionmaker):
    uid,_,_,_,_=await seed(pg_sessionmaker)
    ready=asyncio.Event()
    count=0
    async def attempt():
        nonlocal count
        async with pg_sessionmaker() as s:
            user=await s.get(User,uid)
            count+=1
            if count==2: ready.set()
            await ready.wait()
            try:
                return await borrow(BorrowRequest(amount='600'),user=user,db=s)
            except HTTPException as ex:
                return ex
    results=await asyncio.wait_for(asyncio.gather(attempt(),attempt()),10)
    assert sum(not isinstance(x,HTTPException) for x in results)==1,results
    assert [x.status_code for x in results if isinstance(x,HTTPException)]==[400]
    async with pg_sessionmaker() as s:
        u=await s.get(User,uid)
        assert u.cash==D('600') and u.debt==D('600') and u.economic_version==1
    assert not GATES.held_keys()

async def test_waiting_loan_releases_pg_connection_and_independent_pair_trades(pg_sessionmaker,pg_engine):
    uid,vid,mid,pid,other=await seed(pg_sessionmaker)
    checked_out=0
    def checkout(*args):
        nonlocal checked_out
        checked_out+=1
    def checkin(*args):
        nonlocal checked_out
        checked_out-=1
    event.listen(pg_engine.sync_engine,'checkout',checkout)
    event.listen(pg_engine.sync_engine,'checkin',checkin)
    async def waiting_loan():
        async with pg_sessionmaker() as s:
            return await borrow(BorrowRequest(amount='10'),user=await s.get(User,uid),db=s)
    try:
        async with GATES.hold(exclusive=[GroupKey('lmsr',mid),GroupKey('fx',pid)]):
            waiter=asyncio.create_task(waiting_loan())
            try:
                async with asyncio.timeout(5):
                    while not GATES.waiting_count(): await asyncio.sleep(.005)
                assert checked_out==0, 'Gate waiter retained a PG connection'
                async with pg_sessionmaker() as s:
                    result=await asyncio.wait_for(asyncio.create_task(execute_trade(s,vid,other,'buy',D('1'),D('0'),'independent')),5)
                    assert result.output_amount>0
                assert not waiter.done(), 'Blocked collateral loan must remain blocked'
            except BaseException:
                waiter.cancel()
                await asyncio.gather(waiter,return_exceptions=True)
                raise
        result=await asyncio.wait_for(waiter,5)
        assert result.debt==D('10')
        assert checked_out==0 and not GATES.held_keys()
    finally:
        event.remove(pg_engine.sync_engine,'checkout',checkout)
        event.remove(pg_engine.sync_engine,'checkin',checkin)
