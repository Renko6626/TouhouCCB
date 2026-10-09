"""Disposable PostgreSQL liquidation acceptance; not a production SLO."""
import asyncio
import json
import time
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
import pytest
import pytest_asyncio
from sqlalchemy import select, func
from app.models.base import User, Market, MarketStatus, Outcome, Position, SiteConfig, LiquidationEvent
from app.models.credit import LiquidationAction
from app.models.fx import FxPair, FxWallet, FxTreasury, FxTrade
from app.services import site_config, market_writer, writer_ops
from app.services.credit import execution, sweep, flags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import OWNERSHIP
from app.services.fx.trading import execute_trade, execute_liquidation_sell_in_session
pytestmark = [pytest.mark.pg, pytest.mark.asyncio]

@pytest_asyncio.fixture(autouse=True)
async def unified(pg_sessionmaker, monkeypatch):
    for module in (execution, sweep, market_writer, writer_ops):monkeypatch.setattr(module,'async_session_maker',pg_sessionmaker)
    monkeypatch.setattr(OWNERSHIP,'_writes_enabled',True)
    flags.set_flags(flags.CreditFlags(unified_credit_enabled=True,credit_leverage=D('20'),credit_maintenance_ratio=D('.04')))
    site_config.clear_cache()
    yield
    await market_writer.WRITER.stop()
    flags.clear_flags();site_config.clear_cache()

async def seed(factory,count=1,mixed=False):
    async with factory() as s:
        pairs=[FxPair(currency_code=c,currency_name=c,status='trading',gold_reserve=D('1000000'),foreign_reserve=D('5000000'),sell_fee_rate=D('0')) for c in ('MORA','LMD','THIRD')]
        m=Market(title='mixed',status=MarketStatus.TRADING,liquidity_b=100000)
        users=[User(username=f'debtor{i}',cash=D('10'),debt=D('2000'),tos_accepted_at=datetime.now(timezone.utc)) for i in range(count)]
        t=User(username='trader',cash=D('10000'),tos_accepted_at=datetime.now(timezone.utc))
        s.add_all([*pairs,m,*users,t]);await s.flush()
        a=Outcome(market_id=m.id,label='A',total_shares=D('0'));b=Outcome(market_id=m.id,label='B',total_shares=D('0'))
        s.add_all([a,b]);await s.flush()
        for i,u in enumerate(users):
            for p in pairs[:3 if mixed else 1]:s.add(FxWallet(user_id=u.id,pair_id=p.id,foreign_amount=D('100' if mixed and i%2 else '500')))
            if mixed:
                amount=D('1000' if i%2 else '10');a.total_shares+=amount
                s.add(Position(user_id=u.id,outcome_id=a.id,amount=amount))
        s.add_all([FxTreasury(pair_id=p.id) for p in pairs])
        for k,v in [('loan_enabled','true'),('fx_enabled','true'),('loan_daily_rate','0'),('liquidation_enabled','true'),('liquidation_partial_pct','0.1'),('liquidation_sweep_interval_sec','600')]:s.add(SiteConfig(key=k,value=v,value_type='string'))
        await s.commit()
        return [u.id for u in users],t.id,[p.id for p in pairs]

async def liquidate(uid):return await execution.execute_user(uid,rate=D('0'),pct=D('.1'),source='scheduler')

async def test_pg_liquidation_rollback_retry_and_fx_replay(pg_sessionmaker,monkeypatch):
    ids,_,pairs=await seed(pg_sessionmaker)
    original=execution.record_action
    async def crash(*args,**kwargs):raise RuntimeError('crash before action')
    monkeypatch.setattr(execution,'record_action',crash)
    with pytest.raises(RuntimeError,match='crash before action'):await liquidate(ids[0])
    async with pg_sessionmaker() as s:
        u=await s.get(User,ids[0]);assert (u.cash,u.debt)==(D('10'),D('2000'))
        assert (await s.execute(select(FxWallet))).scalar_one().foreign_amount==500
        for model in (LiquidationAction,LiquidationEvent,FxTrade):assert await s.scalar(select(func.count()).select_from(model))==0
    monkeypatch.setattr(execution,'record_action',original)
    assert await liquidate(ids[0])=='triggered'
    async with pg_sessionmaker() as s:
        a=(await s.execute(select(LiquidationAction))).scalar_one()
        u=await s.get(User,ids[0]);before=(u.cash,u.debt);run_id,round_no=a.run_id,a.round_no
    async with GATES.hold(exclusive=[GroupKey('fx',pairs[0])]):
        async with pg_sessionmaker() as s:
            async with s.begin():
                replay=await execute_liquidation_sell_in_session(s,user_id=ids[0],pair_id=pairs[0],run_id=run_id,round_no=round_no,mode='full',partial_pct=D('.1'))
                assert replay.replay
    async with pg_sessionmaker() as s:
        u=await s.get(User,ids[0]);assert (u.cash,u.debt)==before
        for model in (LiquidationAction,LiquidationEvent,FxTrade):assert await s.scalar(select(func.count()).select_from(model))==1

async def test_pg_liquidation_repayment_contention_and_other_symbol_progress(pg_sessionmaker):
    from app.api.v1.loan import repay
    from app.schemas.loan import RepayRequest
    ids,trader,pairs=await seed(pg_sessionmaker)
    async with GATES.hold(exclusive=[GroupKey('fx',pairs[0])]):
        waiter=asyncio.create_task(liquidate(ids[0]))
        try:
            async with asyncio.timeout(5):
                while not GATES.waiting_count():await asyncio.sleep(.005)
            async with pg_sessionmaker() as s:payment=await asyncio.wait_for(repay(RepayRequest(amount='5'),user=await s.get(User,ids[0]),db=s),5)
            async with pg_sessionmaker() as s:
                trade=await asyncio.wait_for(asyncio.create_task(execute_trade(s,trader,pairs[1],'buy',D('1'),D('0'),'independent')),5)
                assert trade.output_amount>0
            assert not waiter.done()
        except BaseException:
            waiter.cancel();await asyncio.gather(waiter,return_exceptions=True);raise
    assert await asyncio.wait_for(waiter,10)=='triggered'
    async with pg_sessionmaker() as s:
        u=await s.get(User,ids[0]);a=(await s.execute(select(LiquidationAction))).scalar_one()
        assert u.debt==D('2000')-a.repaid-payment.effective
        assert u.cash==D('10')+a.proceeds-a.repaid-payment.effective
        assert await s.scalar(select(func.count()).select_from(LiquidationEvent))==1
    assert not GATES.held_keys()

async def test_pg_100_user_mixed_scan_with_concurrent_trades(pg_sessionmaker):
    ids,trader,pairs=await seed(pg_sessionmaker,100,mixed=True)
    await market_writer.WRITER.start()
    async def trades():
        for i in range(30):
            async with pg_sessionmaker() as s:
                result=await execute_trade(s,trader,pairs[i%3],'buy',D('1'),D('0'),f'mixed-{i}')
                assert result.output_amount>0
        return 30
    start=time.monotonic()
    result,count=await asyncio.wait_for(asyncio.gather(sweep.run_sweep(),trades()),120)
    elapsed=time.monotonic()-start
    assert result['scanned_count']==100 and result['triggered_count']==100
    assert result['errors']==0 and result['deadlocks']==0 and elapsed<600
    async with pg_sessionmaker() as s:
        actions=list((await s.execute(select(LiquidationAction))).scalars())
        assert len(actions)==100 and {a.product for a in actions}=={'fx','lmsr'}
        assert len({a.user_id for a in actions})==100
        assert await s.scalar(select(func.count()).select_from(LiquidationEvent))==100
    evidence={'users':100,'fx_pairs':3,'lmsr_markets':1,'concurrent_fx_buys':count,'scan':result,'total_wall_sec':elapsed,'scope':'One real PG scan, three bounded workers, FX/LMSR liquidation, concurrent debt-free FX buys. No HTTP/WebSocket, production DB, sustained load or concurrent LMSR player orders.'}
    path=Path(__file__).resolve().parents[3]/'.superpowers/sdd/2026-09-30-unified-credit-risk/perf/pg-mixed-scan.json'
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(evidence,indent=2)+'\n')
    print(json.dumps(evidence))
