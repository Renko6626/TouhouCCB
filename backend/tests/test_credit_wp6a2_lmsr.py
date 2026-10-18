from decimal import Decimal as D
import pytest
import pytest_asyncio
from fastapi import HTTPException
from app.core.database import async_session_maker
from app.models.base import User, Market, Outcome, Position, MarketStatus
from app.services.market_writer import WRITER
from app.services.writer_ops import BuyCmd, SellCmd
from app.services.credit.flags import CreditFlags, set_flags
from app.services.credit.ownership import OWNERSHIP

@pytest_asyncio.fixture(autouse=True)
async def lifecycle(setup_db, published_markets):
    await OWNERSHIP.acquire()
    set_flags(CreditFlags( credit_leverage=D('20'), credit_maintenance_ratio=D('.04')))
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

@pytest.mark.asyncio
@pytest.mark.parametrize("auto_repay", [True, False])
async def test_selling_frozen_debtor_reduces_debt(auto_repay):
    uid,mid,oid=await seed(cash='0',debt='100',held='20')
    async with async_session_maker() as s:
        u=await s.get(User,uid); u.credit_frozen=True; u.auto_repay=auto_repay; await s.commit()
    await WRITER.start()
    await WRITER.submit(SellCmd(mid,oid,uid,'riskbuyer',D('10'),None,None,True))
    async with async_session_maker() as s:
        u=await s.get(User,uid)
        if auto_repay:
            assert u.debt < D('100')
            assert u.cash == 0
        else:
            assert u.debt == D('100')
            assert u.cash > 0
        assert u.economic_version > 0


@pytest.mark.asyncio
async def test_buy_uses_fresh_account_and_full_published_collateral(monkeypatch):
    from app.services.credit import risk
    from app.services import writer_ops
    from app.services.credit.keys import GroupKey
    from tests.conftest import publish_seeded_market_records
    uid, mid, oid = await seed(cash='120', debt='100')
    async with async_session_maker() as s:
        m = Market(title='collateral', liquidity_b=100, status=MarketStatus.TRADING)
        s.add(m); await s.flush()
        a = Outcome(market_id=m.id, label='a', total_shares=D('50'))
        b = Outcome(market_id=m.id, label='b', total_shares=D('0'))
        s.add_all([a,b]); await s.flush()
        s.add(Position(user_id=uid, outcome_id=a.id, amount=D('50'), cost_basis=D('0')))
        await s.commit(); other = m.id; collateral_oid = a.id
    await publish_seeded_market_records()
    lock = writer_ops.lock_user
    async def changed_account(session, user_id):
        async with async_session_maker() as s:
            user = await s.get(User, user_id)
            user.cash += D('1'); user.economic_version += 1
            await s.commit()
        return await lock(session, user_id)
    monkeypatch.setattr(writer_ops, 'lock_user', changed_account)
    check = risk.check_new_risk
    async def observing(*args, **kwargs):
        deps = kwargs['deps']
        assert deps.cash == D('121') and deps.economic_version == 1
        assert deps.holdings[GroupKey('lmsr', other)] == {collateral_oid: D('50')}
        assert deps.snapshots[GroupKey('lmsr', other)].outcomes[0].total_shares == D('50')
        assert kwargs['post'].lmsr_q[mid][0] == D('10')
        assert kwargs['post'].post_holdings[GroupKey('lmsr', mid)][oid] == D('10')
        return await check(*args, **kwargs)
    monkeypatch.setattr(risk, 'check_new_risk', observing)
    await WRITER.start()
    await WRITER.submit(buy(uid, mid, oid))
    async with async_session_maker() as s:
        user = await s.get(User, uid)
        assert D('115') < user.cash < D('116')
        assert user.debt == D('100') and user.economic_version == 2
        assert (await s.get(Outcome, oid)).total_shares == D('10')


@pytest.mark.asyncio
async def test_mirror_failure_isolates_committed_market(monkeypatch):
    uid,mid,oid=await seed()
    await WRITER.start()
    async def recovery(market_id):
        assert WRITER.get_state(market_id).unavailable
    def broken(st,outcome):
        raise RuntimeError('mirror apply failure')
    monkeypatch.setattr(WRITER,'_apply_outcome',broken)
    # Consumer recovery uses the private loader; the public method queues a command.
    monkeypatch.setattr(WRITER,'_reload_state',recovery)
    with pytest.raises(HTTPException) as err:
        await WRITER.submit(buy(uid,mid,oid))
    assert err.value.status_code==500
    async with async_session_maker() as s:
        assert (await s.get(User,uid)).cash < D('100')

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
    from tests.conftest import publish_seeded_market_records
    await publish_seeded_market_records()
    check=risk.check_new_risk
    async def observing(*args,**kwargs):
        deps = kwargs['deps']
        assert deps.holdings[GroupKey('fx',pid)] == {pid: D('100')}
        assert deps.snapshots[GroupKey('fx',pid)].pair.gold_reserve == D('1000')
        return await check(*args,**kwargs)
    monkeypatch.setattr(risk,'check_new_risk',observing)
    await WRITER.start()
    await WRITER.submit(buy(uid,mid,oid))
    async with async_session_maker() as s:
        user = await s.get(User, uid)
        assert D('94') < user.cash < D('95')
        assert user.debt == D('100') and user.economic_version == 1
        assert (await s.get(Outcome, oid)).total_shares == D('10')

@pytest.mark.asyncio
async def test_zero_payout_settlement_versions_winner():
    from app.services.writer_ops import ResolveCmd
    uid,mid,oid=await seed(held='10')
    await WRITER.start()
    await WRITER.submit(ResolveCmd(mid,oid,D('0'),uid))
    async with async_session_maker() as s:
        assert (await s.get(User,uid)).economic_version == 1
