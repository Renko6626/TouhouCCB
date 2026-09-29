from decimal import Decimal as D
import pytest
import pytest_asyncio
from fastapi import HTTPException
from app.core.database import async_session_maker
from app.models.base import User, Market, Outcome, Position, MarketStatus
from app.services.market_writer import WRITER
from app.services.writer_ops import BuyCmd, SellCmd
from app.services.credit.flags import CreditFlags, set_flags
from app.services.credit.gates import GATES
from app.services.credit.ownership import OWNERSHIP

@pytest_asyncio.fixture(autouse=True)
async def lifecycle():
    await OWNERSHIP.acquire()
    set_flags(CreditFlags(unified_credit_enabled=True, credit_leverage=D('20'), credit_maintenance_ratio=D('.04')))
    yield
    await WRITER.stop()
    set_flags(CreditFlags())

async def seed(cash='100', debt='0', held='0'):
    async with async_session_maker() as s:
        u = User(username='riskbuyer', casdoor_id='riskbuyer', cash=D(cash), debt=D(debt))
        m = Market(title='risk', liquidity_b=100, status=MarketStatus.TRADING)
        s.add_all([u,m]); await s.flush()
        a = Outcome(market_id=m.id,label='a',total_shares=D(held))
        b = Outcome(market_id=m.id,label='b',total_shares=D('0'))
        s.add_all([a,b]); await s.flush()
        if D(held): s.add(Position(user_id=u.id,outcome_id=a.id,amount=D(held),cost_basis=D('0')))
        await s.commit()
        return u.id,m.id,a.id

def buy(uid,mid,oid):
    return BuyCmd(mid,oid,uid,'riskbuyer',D('10'),None,None,True)

@pytest.mark.asyncio
async def test_debtor_buy_rejects_insufficient_real_equity():
    uid,mid,oid=await seed(cash='100',debt='100')
    await WRITER.start()
    with pytest.raises(HTTPException) as err:
        await WRITER.submit(buy(uid,mid,oid))
    assert err.value.detail == 'insufficient_initial_margin'
    async with async_session_maker() as s:
        assert (await s.get(User,uid)).cash == D('100')
        assert (await s.get(Outcome,oid)).total_shares == 0

@pytest.mark.asyncio
async def test_debt_free_buy_no_dependency_discovery(monkeypatch):
    from app.services.credit import risk
    async def forbidden(*args,**kwargs): raise AssertionError('debt-free full discovery')
    monkeypatch.setattr(risk,'discover_dependencies',forbidden)
    uid,mid,oid=await seed()
    await WRITER.start()
    await WRITER.submit(buy(uid,mid,oid))
    async with async_session_maker() as s:
        assert (await s.get(User,uid)).economic_version == 1
    assert not GATES.held_keys()

@pytest.mark.asyncio
async def test_selling_frozen_debtor_reduces_debt():
    uid,mid,oid=await seed(cash='0',debt='100',held='20')
    async with async_session_maker() as s:
        u=await s.get(User,uid); u.credit_frozen=True; await s.commit()
    await WRITER.start()
    await WRITER.submit(SellCmd(mid,oid,uid,'riskbuyer',D('10'),None,None,True))
    async with async_session_maker() as s:
        u=await s.get(User,uid)
        assert u.debt < D('100')
        assert u.cash == 0
        assert u.economic_version > 0

@pytest.mark.asyncio
async def test_legacy_debtor_rejected_and_debt_free_versioned():
    from app.api.v1.market import buy_shares
    from app.schemas.market import TradeRequest
    uid,mid,oid=await seed(cash='100',debt='100')
    async with async_session_maker() as s:
        u=await s.get(User,uid)
        with pytest.raises(HTTPException) as err:
            await buy_shares(TradeRequest(outcome_id=oid,shares=D('10'),accept_any_slippage=True),u,s)
        assert err.value.detail == 'insufficient_initial_margin'
    async with async_session_maker() as s:
        u=await s.get(User,uid); u.debt=D('0'); await s.commit()
        await buy_shares(TradeRequest(outcome_id=oid,shares=D('10'),accept_any_slippage=True),u,s)
    async with async_session_maker() as s:
        assert (await s.get(User,uid)).economic_version == 1

@pytest.mark.asyncio
async def test_full_collateral_gates_and_version_retry(monkeypatch):
    from app.services.credit import risk
    from app.services.credit.keys import GroupKey
    uid,mid,oid=await seed(cash='120',debt='100')
    async with async_session_maker() as s:
        m=Market(title='collateral',liquidity_b=100,status=MarketStatus.TRADING)
        s.add(m); await s.flush()
        a=Outcome(market_id=m.id,label='a',total_shares=D('50'))
        b=Outcome(market_id=m.id,label='b',total_shares=D('0'))
        s.add_all([a,b]); await s.flush()
        s.add(Position(user_id=uid,outcome_id=a.id,amount=D('50'),cost_basis=D('0')))
        await s.commit(); other=m.id
    discover=risk.discover_dependencies
    count=0
    async def changing(*args,**kwargs):
        nonlocal count
        assert not GATES.held_keys_by_current_task()
        result=await discover(*args,**kwargs)
        count+=1
        if count==1:
            async with async_session_maker() as s:
                u=await s.get(User,uid); u.cash+=D('1'); u.economic_version+=1; await s.commit()
        return result
    monkeypatch.setattr(risk,'discover_dependencies',changing)
    check=risk.check_new_risk
    async def observing(*args,**kwargs):
        assert GATES.held_keys_by_current_task()==frozenset([GroupKey('lmsr',mid),GroupKey('lmsr',other)])
        assert kwargs['post'].lmsr_q[mid][0] == D('10')
        assert kwargs['post'].post_holdings[GroupKey('lmsr',mid)][oid] == D('10')
        return await check(*args,**kwargs)
    monkeypatch.setattr(risk,'check_new_risk',observing)
    await WRITER.start()
    await WRITER.submit(buy(uid,mid,oid))
    assert count==2
    assert not GATES.held_keys()

@pytest.mark.asyncio
async def test_mirror_failure_isolates_before_gate_release(monkeypatch):
    uid,mid,oid=await seed()
    await WRITER.start()
    async def recovery(market_id):
        assert not GATES.held_keys_by_current_task()
        assert WRITER.get_state(market_id).unavailable
    def broken(st,outcome):
        assert GATES.held_keys_by_current_task()
        raise RuntimeError('mirror apply failure')
    monkeypatch.setattr(WRITER,'_apply_outcome',broken)
    monkeypatch.setattr(WRITER,'reload_state',recovery)
    with pytest.raises(HTTPException) as err:
        await WRITER.submit(buy(uid,mid,oid))
    assert err.value.status_code==500
    async with async_session_maker() as s:
        assert (await s.get(User,uid)).cash < D('100')
    assert not GATES.held_keys()

@pytest.mark.asyncio
async def test_writer_rejects_submit_while_holding_gate():
    from app.services.credit.keys import GroupKey
    uid,mid,oid=await seed()
    await WRITER.start()
    async with GATES.hold(exclusive=[GroupKey('lmsr',mid)]):
        with pytest.raises(RuntimeError,match='Release all'):
            await WRITER.submit(buy(uid,mid,oid))

@pytest.mark.asyncio
async def test_fx_collateral_can_fund_lmsr_admission(monkeypatch):
    from app.models.fx import FxPair, FxWallet
    from app.services.credit import risk
    from app.services.credit.keys import GroupKey
    uid,mid,oid=await seed(cash='100',debt='100')
    async with async_session_maker() as s:
        pair=FxPair(currency_code='MORA',currency_name='Mora',status='trading',
                    gold_reserve=D('1000'),foreign_reserve=D('5000'))
        s.add(pair); await s.flush()
        s.add(FxWallet(user_id=uid,pair_id=pair.id,foreign_amount=D('100'),cost_basis=D('20')))
        await s.commit(); pid=pair.id
    check=risk.check_new_risk
    async def observing(*args,**kwargs):
        assert GroupKey('fx',pid) in GATES.held_keys_by_current_task()
        return await check(*args,**kwargs)
    monkeypatch.setattr(risk,'check_new_risk',observing)
    await WRITER.start()
    await WRITER.submit(buy(uid,mid,oid))

@pytest.mark.asyncio
async def test_zero_payout_settlement_versions_winner():
    from app.services.writer_ops import ResolveCmd
    uid,mid,oid=await seed(held='10')
    await WRITER.start()
    await WRITER.submit(ResolveCmd(mid,oid,D('0'),uid))
    async with async_session_maker() as s:
        assert (await s.get(User,uid)).economic_version == 1

@pytest.mark.asyncio
async def test_legacy_postcommit_config_and_publish_release_gates(monkeypatch):
    import asyncio
    from app.api.v1 import market as api
    from app.schemas.market import TradeRequest
    from app.services.credit.keys import GroupKey
    uid,mid,oid=await seed()
    original=api.site_config.get_bool_or
    seen=[]
    async def config(db,key,default):
        if key == 'legacy_trade_events':
            assert not GATES.held_keys_by_current_task()
            async with GATES.hold(exclusive=[GroupKey('lmsr',mid)]):
                await asyncio.sleep(0)
            seen.append('config')
            return True
        return await original(db,key,default)
    async def publish(topic,event,data):
        assert not GATES.held_keys_by_current_task()
        async with GATES.hold(exclusive=[GroupKey('lmsr',mid)]):
            await asyncio.sleep(0)
        assert topic == f'lmsr:{mid}' and event == 'trade'
        assert data['trade']['shares'] == 10.0
        seen.append('publish')
    monkeypatch.setattr(api.site_config,'get_bool_or',config)
    monkeypatch.setattr(api.BROKER,'publish',publish)
    async with async_session_maker() as s:
        u=await s.get(User,uid)
        result=await api.buy_shares(TradeRequest(outcome_id=oid,shares=D('10'),accept_any_slippage=True),u,s)
    assert result['shares'] == 10.0
    assert seen == ['config','publish']
