from decimal import Decimal as D
import pytest
import pytest_asyncio
from sqlalchemy import select
from app.core.database import async_session_maker
from app.models.base import User, Market, Outcome, Position, LiquidationEvent, MarketStatus
from app.models.fx import FxPair, FxWallet
from app.models.credit import LiquidationAction, LiquidationRun
from app.services.credit.flags import CreditFlags, set_flags
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.execution import execute_user
from app.services.market_writer import WRITER

@pytest_asyncio.fixture(autouse=True)
async def lifecycle():
    await OWNERSHIP.acquire()
    set_flags(CreditFlags( credit_leverage=D('20'), credit_maintenance_ratio=D('.04')))
    yield
    await WRITER.stop()
    set_flags(CreditFlags())

async def seed(cash='0',debt='100',fx=None,lmsr=None,status='trading'):
    async with async_session_maker() as s:
        u=User(username='liquidatee',casdoor_id='liq',cash=D(cash),debt=D(debt))
        s.add(u);await s.flush()
        if fx:
            for i,amount in enumerate(fx):
                p=FxPair(currency_code=f'FX{i}',currency_name=f'FX{i}',status=status,
                    gold_reserve=D('10000'),foreign_reserve=D('50000'),sell_fee_rate=D('0'))
                s.add(p);await s.flush()
                s.add(FxWallet(user_id=u.id,pair_id=p.id,foreign_amount=D(amount),cost_basis=D('0')))
        if lmsr:
            m=Market(title='liq',liquidity_b=100,status=MarketStatus.TRADING)
            s.add(m);await s.flush()
            a=Outcome(market_id=m.id,label='a',total_shares=D(lmsr));b=Outcome(market_id=m.id,label='b',total_shares=D('0'))
            s.add_all([a,b]);await s.flush()
            s.add(Position(user_id=u.id,outcome_id=a.id,amount=D(lmsr),cost_basis=D('0')))
        await s.commit();return u.id

async def execute(uid):
    return await execute_user(uid,rate=D('0'),pct=D('.1'),source='scheduler')

@pytest.mark.asyncio
async def test_cash_recovers_without_selling_and_event_product_nullable():
    uid=await seed(cash='101',debt='100')
    assert await execute(uid)=='triggered'
    async with async_session_maker() as s:
        u=await s.get(User,uid);assert u.cash==1 and u.debt==0 and u.last_liquidated_at
        a=(await s.execute(select(LiquidationAction))).scalar_one()
        assert a.kind=='repay_cash' and a.repaid==100
        ev=(await s.execute(select(LiquidationEvent))).scalar_one();assert ev.product is None
        assert (await s.execute(select(LiquidationRun))).scalar_one().status=='recovered'
    assert await execute(uid)=='recovered'
    async with async_session_maker() as s:
        assert len(list((await s.execute(select(LiquidationEvent))).scalars()))==1

@pytest.mark.asyncio
async def test_fx_partial_exactly_one_group_and_one_action():
    uid=await seed(debt='135',fx=['500','200'])
    assert await execute(uid)=='triggered'
    async with async_session_maker() as s:
        wallets=list((await s.execute(select(FxWallet).order_by(FxWallet.pair_id))).scalars())
        assert [w.foreign_amount for w in wallets]==[D('450'),D('200')]
        a=(await s.execute(select(LiquidationAction))).scalar_one()
        assert a.mode=='partial' and a.product=='fx' and a.repaid>0
        assert (await s.execute(select(LiquidationEvent))).scalar_one().sold_positions_count==1

@pytest.mark.asyncio
async def test_insolvent_full_selected_group_and_paused_untouched():
    uid=await seed(debt='200',fx=['500'],lmsr='10')
    assert await execute(uid)=='triggered'
    async with async_session_maker() as s:
        assert (await s.execute(select(FxWallet))).scalar_one().foreign_amount==0
        assert (await s.execute(select(Position))).scalar_one().amount==10
        assert (await s.execute(select(LiquidationAction))).scalar_one().mode=='full'

@pytest.mark.asyncio
async def test_paused_debt_preserved_frozen_no_sale():
    uid=await seed(debt='100',fx=['500'],status='paused')
    assert await execute(uid)=='blocked'
    async with async_session_maker() as s:
        u=await s.get(User,uid);assert u.debt==100 and u.credit_frozen
        assert (await s.execute(select(FxWallet))).scalar_one().foreign_amount==500
        assert (await s.execute(select(LiquidationRun))).scalar_one().status=='active'
        assert not list((await s.execute(select(LiquidationEvent))).scalars())

@pytest.mark.asyncio
async def test_lmsr_writer_batch_and_replay_no_duplicate_event():
    uid=await seed(debt='200',lmsr='100')
    await WRITER.start()
    assert await execute(uid)=='triggered'
    async with async_session_maker() as s:
        action=(await s.execute(select(LiquidationAction))).scalar_one()
        from app.services.writer_ops import LiquidateGroupCmd
        cmd=LiquidateGroupCmd(action.group_id,uid,action.run_id,action.round_no,'full',D('.1'),D('0'),revalidate_account=True)
        debt=(await s.get(User,uid)).debt
        assert (await s.execute(select(LiquidationEvent))).scalar_one().product=='lmsr'
    replay=await WRITER.submit(cmd);assert replay['replayed']
    async with async_session_maker() as s:
        assert (await s.get(User,uid)).debt==debt
        assert len(list((await s.execute(select(LiquidationEvent))).scalars()))==1
        assert (await s.get(User,uid)).credit_frozen

@pytest.mark.asyncio
async def test_fx_crash_before_action_rolls_back_and_retry(monkeypatch):
    from app.services.credit import execution
    uid=await seed(debt='200',fx=['500'])
    original=execution.record_action
    async def crash(*args,**kwargs): raise RuntimeError('simulated crash')
    monkeypatch.setattr(execution,'record_action',crash)
    with pytest.raises(RuntimeError,match='simulated crash'):await execute(uid)
    async with async_session_maker() as s:
        assert (await s.get(User,uid)).debt==200
        assert (await s.execute(select(FxWallet))).scalar_one().foreign_amount==500
        assert not list((await s.execute(select(LiquidationAction))).scalars())
    monkeypatch.setattr(execution,'record_action',original)
    assert await execute(uid)=='triggered'
    async with async_session_maker() as s:
        assert len(list((await s.execute(select(LiquidationAction))).scalars()))==1

@pytest.mark.asyncio
async def test_queued_lmsr_rechecks_all_collateral_and_selection(monkeypatch):
    from app.services.credit import execution
    from app.services.credit.gates import GATES
    from app.services.credit.keys import GroupKey
    uid=await seed(debt='500',fx=['200'],lmsr='100')
    await WRITER.start()
    original_submit=WRITER.submit
    changed=False
    async def submit(cmd):
        nonlocal changed
        assert not GATES.held_keys_by_current_task()
        if not changed:
            changed=True
            async with async_session_maker() as s:
                pair=(await s.execute(select(FxPair))).scalar_one()
                pair.gold_reserve=D('100000');pair.pool_version+=1;await s.commit()
        return await original_submit(cmd)
    monkeypatch.setattr(WRITER,'submit',submit)
    original_prepare=execution.prepare_locked
    async def prepare(session,user,deps,**kwargs):
        assert set(deps.groups)<=GATES.held_keys_by_current_task()
        return await original_prepare(session,user,deps,**kwargs)
    monkeypatch.setattr(execution,'prepare_locked',prepare)
    assert await execute(uid)=='triggered'
    async with async_session_maker() as s:
        assert (await s.execute(select(LiquidationAction))).scalar_one().product=='fx'
        assert (await s.execute(select(Position))).scalar_one().amount==100
        assert len(list((await s.execute(select(LiquidationEvent))).scalars()))==1

@pytest.mark.asyncio
async def test_partial_unexecutable_group_falls_through_without_deleting(monkeypatch):
    from app.services.credit import execution
    from dataclasses import replace
    uid=await seed(debt='135',fx=['500','200'])
    original=execution.quote_fx_group
    def quote(pair,**kwargs):
        q=original(pair,**kwargs)
        if pair.pair_id==1 and kwargs['mode']=='partial':
            return replace(q,blocked_reason='batch_too_small')
        return q
    monkeypatch.setattr(execution,'quote_fx_group',quote)
    assert await execute(uid)=='triggered'
    async with async_session_maker() as s:
        wallets=list((await s.execute(select(FxWallet).order_by(FxWallet.pair_id))).scalars())
        assert [w.foreign_amount for w in wallets]==[D('500'),D('180')]


@pytest.mark.asyncio
async def test_paused_resume_between_thresholds_continues_same_run():
    from app.services.credit.valuation import value_user_detailed
    uid=await seed(debt='100',fx=['500'],status='paused')
    assert await execute(uid)=='blocked'
    async with async_session_maker() as s:
        run=(await s.execute(select(LiquidationRun))).scalar_one()
        run_id=run.id
        assert run.status=='active' and run.last_blocked_reason=='no_executable_group'
        pair=(await s.execute(select(FxPair))).scalar_one()
        # 500 / (50000+500) * 10554.5 = 104.5 LCV; E/D=4.5%.
        pair.gold_reserve=D('10554.5');pair.status='trading';pair.pool_version+=1
        await s.commit()
        value=await value_user_detailed(s,uid,daily_rate=D('0'))
        assert D('4') < value.liquidation_equity < D('100')/D('19')
    assert await execute(uid)=='triggered'
    async with async_session_maker() as s:
        actions=list((await s.execute(select(LiquidationAction).order_by(LiquidationAction.round_no))).scalars())
        assert [a.kind for a in actions]==['blocked','sell_group']
        assert all(a.run_id==run_id for a in actions)
        assert (await s.execute(select(FxWallet))).scalar_one().foreign_amount==D('450')

@pytest.mark.asyncio
async def test_last_sale_with_paused_asset_keeps_run_active():
    uid=await seed(debt='300',fx=['500','200'])
    async with async_session_maker() as s:
        pairs=list((await s.execute(select(FxPair).order_by(FxPair.id))).scalars())
        pairs[1].status='paused';await s.commit()
    assert await execute(uid)=='triggered'
    async with async_session_maker() as s:
        run=(await s.execute(select(LiquidationRun))).scalar_one()
        assert run.status=='active' and run.last_blocked_reason=='no_executable_group'
        assert (await s.get(User,uid)).credit_frozen


@pytest.mark.asyncio
async def test_finite_version_conflicts_exhaust_without_mutation_then_resume(monkeypatch, caplog):
    from app.services.credit import execution
    from app.services.credit.flags import get_flags
    from app.models.audit import AuditEvent
    uid = await seed(debt='200', fx=['500'])
    original = execution.lock_user
    attempts = 0
    async def conflict(session, user_id):
        nonlocal attempts
        user = await original(session, user_id)
        # Simulate a conflicting snapshot only; never persist the injected version.
        attempts += 1
        from sqlalchemy.orm.attributes import set_committed_value
        set_committed_value(user, 'economic_version', user.economic_version + 1)
        return user
    monkeypatch.setattr(execution, 'lock_user', conflict)
    with caplog.at_level('INFO', logger=execution.__name__):
        assert await execute(uid) == 'retry_exhausted'
    assert attempts == get_flags().credit_risk_retry_limit + 1
    assert [r.retry_reason for r in caplog.records if hasattr(r, 'retry_reason')] == [
        'economic_version_drift'] * attempts
    async with async_session_maker() as s:
        user = await s.get(User, uid)
        assert (user.cash, user.debt, user.economic_version) == (D('0'), D('200'), 0)
        assert (await s.execute(select(FxWallet))).scalar_one().foreign_amount == 500
        for model in (LiquidationRun, LiquidationAction, LiquidationEvent, AuditEvent):
            assert not list((await s.execute(select(model))).scalars())
    monkeypatch.setattr(execution, 'lock_user', original)
    assert await execute(uid) == 'triggered'
    async with async_session_maker() as s:
        assert (await s.execute(select(FxWallet))).scalar_one().foreign_amount == 0
        assert (await s.get(User, uid)).debt < 200
        assert (await s.execute(select(LiquidationAction))).scalar_one().kind == 'sell_group'
