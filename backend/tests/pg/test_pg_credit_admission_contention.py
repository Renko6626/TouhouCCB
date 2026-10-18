"""Real PG admission serialization and gate-wait resource checks (local disposable DB)."""
import asyncio
from tests.conftest import publish_seeded_market_records
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
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import OWNERSHIP
from app.services.fx.trading import execute_trade

pytestmark = [pytest.mark.pg, pytest.mark.asyncio]

@pytest.fixture(autouse=True)
def unified(monkeypatch):
    flags.set_flags(flags.CreditFlags( credit_leverage=D('4'), credit_maintenance_ratio=D('.1')))
    monkeypatch.setattr(OWNERSHIP, '_writes_enabled', True)
    monkeypatch.setattr(OWNERSHIP, '_is_owner', True)
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
        await publish_seeded_market_records(factory)
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

async def test_loan_uses_previous_publication_while_collateral_writer_is_busy(pg_sessionmaker, monkeypatch):
    from app.services.fx.writer import FX_WRITER
    from app.services.fx import writer_ops
    uid, vid, _, pid, other = await seed(pg_sessionmaker)
    await FX_WRITER.start()
    entered, release = asyncio.Event(), asyncio.Event()
    original = writer_ops._execute_in_session
    async def held(db, command, deps):
        if command.pair_id == pid:
            entered.set()
            await release.wait()
        return await original(db, command, deps)
    monkeypatch.setattr(writer_ops, '_execute_in_session', held)
    async def trade(pair_id, key):
        async with pg_sessionmaker() as db:
            return await execute_trade(db, vid, pair_id, 'buy', D('1'), D('0'), key)
    busy = asyncio.create_task(trade(pid, 'busy-collateral'))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with pg_sessionmaker() as db:
            result = await asyncio.wait_for(borrow(BorrowRequest(amount='10'),
                user=await db.get(User, uid), db=db), 5)
        assert result.debt == D('10') and not busy.done()
        assert (await asyncio.wait_for(trade(other, 'independent'), 5)).output_amount > 0
    finally:
        release.set()
        await asyncio.gather(busy)


async def test_two_market_writers_recheck_same_account_after_user_lock(pg_sessionmaker, monkeypatch):
    from sqlalchemy import delete, select
    from app.services.fx import borrow_buy, trading
    from app.services.fx.writer import FX_WRITER
    uid, _, _, first, second = await seed(pg_sessionmaker)
    async with pg_sessionmaker() as db:
        user = await db.get(User, uid)
        user.cash = D('40')
        await db.execute(delete(Position).where(Position.user_id == uid))
        await db.execute(delete(FxWallet).where(FxWallet.user_id == uid))
        for pid in (first, second):
            pair = await db.get(FxPair, pid)
            pair.buy_fee_rate = pair.sell_fee_rate = D('.01')
        await db.commit()
    await publish_seeded_market_records(pg_sessionmaker)
    await FX_WRITER.start()
    first_locked, second_waiting = asyncio.Event(), asyncio.Event()
    original_lock, original_check = trading.lock_user, trading.check_new_risk
    checked = []
    async def ordered_lock(db, user_id):
        is_first = asyncio.current_task().get_name() == f'fx-writer-{first}'
        if not is_first:
            second_waiting.set()
        user = await original_lock(db, user_id)
        if is_first:
            first_locked.set()
            await second_waiting.wait()
        return user
    async def check(*args, **kwargs):
        deps = kwargs['deps']
        checked.append((deps.cash, deps.debt, dict(deps.holdings), deps.economic_version))
        return await original_check(*args, **kwargs)
    monkeypatch.setattr(trading, 'lock_user', ordered_lock)
    monkeypatch.setattr(trading, 'check_new_risk', check)
    async def financing(pid):
        async with pg_sessionmaker() as db:
            try:
                return await borrow_buy.execute_borrow_buy(db, uid, pid,
                    D('80'), D('70'), D('0'), f'fin-{pid}')
            except HTTPException as exc:
                return exc
    one = asyncio.create_task(financing(first))
    await asyncio.wait_for(first_locked.wait(), 5)
    two = asyncio.create_task(financing(second))
    results = await asyncio.wait_for(asyncio.gather(one, two), 10)
    assert not isinstance(results[0], HTTPException), results
    assert isinstance(results[1], HTTPException) and results[1].detail == 'insufficient_initial_margin'
    assert checked[0] == (D('40'), D('0'), {}, 0)
    cash, debt, holdings, version = checked[1]
    assert (cash, debt, version) == (D('30'), D('70'), 2)
    assert set(holdings) == {GroupKey('fx', first)}
    assert holdings[GroupKey('fx', first)][first] == results[0].output_amount
    async with pg_sessionmaker() as db:
        user = await db.get(User, uid)
        assert (user.cash, user.debt, user.economic_version) == (D('30'), D('70'), 2)
        wallets = (await db.execute(select(FxWallet).where(FxWallet.user_id == uid))).scalars().all()
        assert [(w.pair_id, w.foreign_amount) for w in wallets] == [(first, results[0].output_amount)]


async def test_stale_prefetch_cannot_reuse_cleared_collateral_or_omit_new_debt(pg_sessionmaker):
    from sqlalchemy import delete
    from app.models.fx import FxShortPosition
    from app.services.credit.risk import discover_dependencies, check_cash_spend
    from app.services.market_locks import lock_user
    uid, _, _, pid, _ = await seed(pg_sessionmaker)
    async with pg_sessionmaker() as stale_session:
        stale = await discover_dependencies(stale_session, uid)
        assert stale.holdings
        await stale_session.rollback()
        async with pg_sessionmaker() as db:
            user = await lock_user(db, uid)
            await db.execute(delete(Position).where(Position.user_id == uid))
            await db.execute(delete(FxWallet).where(FxWallet.user_id == uid))
            user.cash, user.debt, user.economic_version = D('30'), D('70'), 1
            db.add(FxShortPosition(user_id=uid, pair_id=pid,
                principal_foreign=D('10'), interest_foreign=D('0'),
                interest_last_accrued_at=datetime.now(timezone.utc)))
            await db.commit()
        user = await lock_user(stale_session, uid)
        result = await check_cash_spend(stale_session, user=user, deps=stale,
            spend=D('5'), thresholds=flags.get_flags().thresholds,
            partial_pct=D('1'), now=datetime.now(timezone.utc))
        assert not result.allowed and result.reason == 'insufficient_initial_margin'
        assert result.debt_after == D('70') and result.short_debt_after == D('10')
        assert result.short_cover_cost > 0
        assert result.equity_after == D('-45') - result.short_cover_cost
        await stale_session.rollback()
    async with pg_sessionmaker() as db:
        user = await db.get(User, uid)
        assert (user.cash, user.debt, user.economic_version) == (D('30'), D('70'), 1)
