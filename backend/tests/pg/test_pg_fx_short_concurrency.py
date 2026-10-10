"""PG races catch cross-purpose duplicate execution and spending locked proceeds.

Barriers keep production GATES/row locks intact. Ownership is acquired on the
same disposable database used by the PG fixtures.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import admin_fx, loan
from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.models.fx import FxPair, FxShortPosition, FxTrade, FxTreasury, FxWallet
from app.models.redemption import DanmukuExchange
from app.schemas.loan import BorrowRequest
from app.services import danmuku, loan_sweep, site_config
from app.services.credit import flags, ownership
from app.services.credit.gates import GATES
from app.services.fx import shorts, trading
from app.services.fx.amm import quote_buy_exact_out, quote_sell

pytestmark = [pytest.mark.pg, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def writer(pg_engine, monkeypatch):
    owner = ownership.WriteOwnership(url=os.environ['TEST_PG_DATABASE_URL'])
    assert await owner.acquire(required=True)
    for module in (ownership, shorts, trading, loan, admin_fx, loan_sweep, danmuku):
        monkeypatch.setattr(module, 'OWNERSHIP', owner)
    flags.set_flags(flags.CreditFlags(
                    credit_leverage=D('4'), credit_maintenance_ratio=D('.1')))
    site_config.clear_cache()
    try:
        yield
        assert not GATES.held_keys()
    finally:
        flags.clear_flags()
        flags.set_new_risk_frozen(None)
        site_config.clear_cache()
        await owner.release()


async def seed(factory, *, cash='1000', debt='0'):
    async with factory() as db:
        user = User(username='race', cash=D(cash), debt=D(debt),
                    tos_accepted_at=datetime.now(timezone.utc))
        pair = FxPair(currency_code='RACE', currency_name='Race', status='trading',
                      gold_reserve=D('1000'), foreign_reserve=D('1000'),
                      buy_fee_rate=D('.01'), sell_fee_rate=D('.01'),
                      short_lending_limit_foreign=D('10000'))
        db.add_all([user, pair])
        await db.flush()
        db.add(FxTreasury(pair_id=pair.id, gold_balance=D('1000'),
                          foreign_balance=D('10000')))
        for key, value, kind in [('fx_enabled', 'true', 'bool'),
                                 ('loan_enabled', 'true', 'bool'),
                                 ('fx_short_enabled', 'true', 'bool'),
                                 ('loan_daily_rate', '0', 'decimal')]:
            db.add(SiteConfig(key=key, value=value, value_type=kind))
        await db.commit()
        return user.id, pair.id


async def state(factory, uid, pid):
    async with factory() as db:
        user = await db.get(User, uid)
        pair = await db.get(FxPair, pid)
        treasury = (await db.execute(select(FxTreasury).where(
            FxTreasury.pair_id == pid))).scalar_one()
        positions = (await db.execute(select(FxShortPosition).where(
            FxShortPosition.user_id == uid))).scalars().all()
        wallets = (await db.execute(select(FxWallet).where(
            FxWallet.user_id == uid))).scalars().all()
        trades = (await db.execute(select(FxTrade).order_by(FxTrade.id))).scalars().all()
        audits = (await db.execute(select(AuditEvent).order_by(AuditEvent.id))).scalars().all()
        locked = sum((p.restricted_gold for p in positions), D('0'))
        assert D('0') <= locked <= user.cash
        assert not (any(w.foreign_amount > 0 for w in wallets)
                    and any(p.principal_foreign + p.interest_foreign > 0 for p in positions))
        return dict(cash=user.cash, debt=user.debt, locked=locked,
                    version=user.economic_version, pool_version=pair.pool_version,
                    principal=sum((p.principal_foreign for p in positions), D('0')),
                    interest=sum((p.interest_foreign for p in positions), D('0')),
                    gold=pair.gold_reserve + treasury.gold_balance + user.cash,
                    foreign=pair.foreign_reserve + treasury.foreign_balance
                            + sum((w.foreign_amount for w in wallets), D('0')),
                    treasury_foreign=treasury.foreign_balance,
                    trades=[(t.id, t.purpose, t.input_amount, t.output_amount) for t in trades],
                    audits=[(a.id, a.event_type, a.ref_id) for a in audits])


async def open_short(db, uid, pid, key='shared'):
    return await shorts.execute_short_open(db, user_id=uid, pair_id=pid,
        foreign_amount=D('100'), min_gold_out=D('0'), idempotency_key=key)


async def spot_buy(db, uid, pid, key='shared'):
    return await trading.execute_trade(db, uid, pid, 'buy', D('100'), D('0'), key)


@pytest.mark.parametrize('first', ['short', 'spot'])
async def test_same_key_short_and_spot_execute_only_one_ledger(
        pg_sessionmaker, monkeypatch, first):
    uid, pid = await seed(pg_sessionmaker)
    before = await state(pg_sessionmaker, uid, pid)
    leader_holds_gate, contender_reached_gate = asyncio.Event(), asyncio.Event()
    original_hold = GATES.hold

    @asynccontextmanager
    async def ordered_hold(*args, **kwargs):
        leader = asyncio.current_task().get_name() == 'leader'
        if not leader:
            contender_reached_gate.set()
        async with original_hold(*args, **kwargs):
            if leader:
                leader_holds_gate.set()
                await contender_reached_gate.wait()
            yield

    monkeypatch.setattr(GATES, 'hold', ordered_hold)
    operations = (open_short, spot_buy) if first == 'short' else (spot_buy, open_short)

    async def attempt(operation):
        async with pg_sessionmaker() as db:
            try:
                return await operation(db, uid, pid)
            except HTTPException as exc:
                return exc

    async with asyncio.timeout(10):
        leader = asyncio.create_task(attempt(operations[0]), name='leader')
        await leader_holds_gate.wait()
        contender = asyncio.create_task(attempt(operations[1]), name='contender')
        results = await asyncio.gather(leader, contender)
    assert not isinstance(results[0], HTTPException), results
    assert isinstance(results[1], HTTPException) and results[1].status_code == 409, results
    after = await state(pg_sessionmaker, uid, pid)
    assert after['gold'] == before['gold'] and after['foreign'] == before['foreign']
    assert len(after['trades']) == 1
    trade_id, purpose, _, _ = after['trades'][0]
    assert purpose == ('short_open' if first == 'short' else 'spot')
    assert [(a[1], a[2]) for a in after['audits'] if a[1] == 'fx_trade'] == [('fx_trade', trade_id)]
    assert after['debt'] == 0 and after['version'] == 1
    async with pg_sessionmaker() as db:
        await operations[0](db, uid, pid)
    assert await state(pg_sessionmaker, uid, pid) == after


async def test_cover_waits_for_gold_repayment_and_replays_without_double_return(
        pg_sessionmaker, monkeypatch):
    uid, pid = await seed(pg_sessionmaker, cash='200')
    async with pg_sessionmaker() as db:
        await open_short(db, uid, pid, 'initial')
    # Existing gold debt can leave an account under margin after other losses.
    # Seed that persisted state directly: both operations below reduce debt.
    async with pg_sessionmaker() as db:
        user = await db.get(User, uid)
        user.debt = D('500')
        await db.commit()
    before = await state(pg_sessionmaker, uid, pid)
    repay_has_user, cover_has_pair = asyncio.Event(), asyncio.Event()
    original_user_lock, original_pair_lock = loan.lock_user, shorts._lock_pair

    async def hold_repay_user(db, user_id):
        user = await original_user_lock(db, user_id)
        repay_has_user.set()
        await cover_has_pair.wait()
        return user

    async def observe_cover_pair(db, pair_id):
        pair = await original_pair_lock(db, pair_id)
        cover_has_pair.set()
        return pair

    monkeypatch.setattr(loan, 'lock_user', hold_repay_user)
    monkeypatch.setattr(shorts, '_lock_pair', observe_cover_pair)

    async def repayment():
        async with pg_sessionmaker() as db:
            return await loan.repay_all(user=await db.get(User, uid), db=db)

    async def cover():
        async with pg_sessionmaker() as db:
            return await shorts.execute_short_cover(db, user_id=uid, pair_id=pid,
                foreign_amount=D('40'), cover_all=False, max_gold_in=D('100'),
                idempotency_key='cover')

    async with asyncio.timeout(10):
        repayment_task = asyncio.create_task(repayment())
        await repay_has_user.wait()
        cover_task = asyncio.create_task(cover())
        repayment_result, cover_result = await asyncio.gather(repayment_task, cover_task)
    after = await state(pg_sessionmaker, uid, pid)
    assert repayment_result.effective == D('200')
    assert after['debt'] == D('300') and after['principal'] == D('60')
    assert after['interest'] == 0
    assert after['cash'] == before['cash'] - D('200') - cover_result.input_amount
    assert after['gold'] == before['gold'] - D('200')
    assert after['foreign'] == before['foreign']
    assert after['treasury_foreign'] == before['treasury_foreign'] + D('40')
    assert D('0') < after['locked'] < before['locked']
    assert before['locked'] - after['locked'] >= cover_result.input_amount
    assert [t[1] for t in after['trades']] == ['short_open', 'short_cover']
    assert sum(a[1] == 'loan_repay' for a in after['audits']) == 1
    assert sum(a[1] == 'fx_trade' for a in after['audits']) == 2
    assert after['version'] == before['version'] + 2
    replay = await cover()
    assert replay.replay
    assert await state(pg_sessionmaker, uid, pid) == after


@pytest.mark.parametrize('status,min_out,expected_code', [
    ('paused', D('0'), 403), ('trading', D('85'), 409),
])
async def test_pair_mutation_rejects_stale_short_open(
        pg_sessionmaker, monkeypatch, status, min_out, expected_code):
    """An old order must honor a gated pause or the newly worse sell fee."""
    uid, pid = await seed(pg_sessionmaker)
    before = await state(pg_sessionmaker, uid, pid)
    if status == 'trading':
        old = quote_sell(D('100'), D('1000'), D('1000'), D('.01'))
        new = quote_sell(D('100'), D('1000'), D('1000'), D('.1'))
        assert new.output_amount < min_out < old.output_amount
    order_discovered, mutation_committed = asyncio.Event(), asyncio.Event()
    original_hold = GATES.hold

    @asynccontextmanager
    async def delay_order_gate(*args, **kwargs):
        if asyncio.current_task().get_name() == 'stale-open':
            order_discovered.set()
            await mutation_committed.wait()
        async with original_hold(*args, **kwargs):
            yield

    monkeypatch.setattr(GATES, 'hold', delay_order_gate)

    async def opening():
        async with pg_sessionmaker() as db:
            try:
                return await shorts.execute_short_open(db, user_id=uid, pair_id=pid,
                    foreign_amount=D('100'), min_gold_out=min_out,
                    idempotency_key='stale-open')
            except HTTPException as exc:
                return exc

    async def mutation():
        await order_discovered.wait()
        async with pg_sessionmaker() as db:
            admin = await db.get(User, uid)
            await admin_fx.update_pair(pid, admin_fx.PairPatch(
                status=status, reduce_only=(status == 'paused'),
                buy_fee_rate=D('.05'), sell_fee_rate=D('.1')),
                admin=admin, db=db)
        mutation_committed.set()

    async with asyncio.timeout(10):
        opening_task = asyncio.create_task(opening(), name='stale-open')
        mutation_task = asyncio.create_task(mutation())
        result, _ = await asyncio.gather(opening_task, mutation_task)
    assert isinstance(result, HTTPException) and result.status_code == expected_code, result
    after = await state(pg_sessionmaker, uid, pid)
    for field in ('cash', 'debt', 'locked', 'principal', 'interest', 'gold',
                  'foreign', 'treasury_foreign', 'trades', 'version'):
        assert after[field] == before[field], field
    assert not any(a[1] == 'fx_trade' for a in after['audits'])
    async with pg_sessionmaker() as db:
        pair = await db.get(FxPair, pid)
        assert pair.status == status
        assert pair.reduce_only == (status == 'paused')
        assert pair.buy_fee_rate == D('.05') and pair.sell_fee_rate == D('.1')
        assert pair.pool_version == before['pool_version'] + 1


async def test_scheduled_foreign_interest_and_cover_all_settle_once(
        pg_sessionmaker, pg_engine, monkeypatch):
    """A scheduler holding User must finish before cover repays its new debt."""
    uid, pid = await seed(pg_sessionmaker)
    async with pg_sessionmaker() as db:
        await open_short(db, uid, pid, 'initial')
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    async with pg_sessionmaker() as db:
        position = (await db.execute(select(FxShortPosition).where(
            FxShortPosition.user_id == uid))).scalar_one()
        position.interest_last_accrued_at = now - timedelta(days=1)
        config = (await db.execute(select(SiteConfig).where(
            SiteConfig.key == 'loan_daily_rate'))).scalar_one()
        config.value = '.01'
        await db.commit()
    site_config.clear_cache()
    before = await state(pg_sessionmaker, uid, pid)
    scheduler_has_user, cover_has_pair = asyncio.Event(), asyncio.Event()
    original_pair_lock = shorts._lock_pair

    class SchedulerSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            descriptions = getattr(statement, 'column_descriptions', [])
            if (getattr(statement, '_for_update_arg', None) is not None
                    and descriptions and descriptions[0].get('entity') is User):
                scheduler_has_user.set()
                await cover_has_pair.wait()
            return result

    def scheduler_session():
        return SchedulerSession(pg_engine, expire_on_commit=False)

    async def observe_cover_pair(db, pair_id):
        pair = await original_pair_lock(db, pair_id)
        cover_has_pair.set()
        return pair

    monkeypatch.setattr(loan_sweep, 'async_session_maker', scheduler_session)
    monkeypatch.setattr(loan_sweep, '_compat_now', lambda user: now)
    monkeypatch.setattr(shorts, 'utcnow', lambda: now)
    monkeypatch.setattr(shorts, '_lock_pair', observe_cover_pair)

    async def cover():
        async with pg_sessionmaker() as db:
            return await shorts.execute_short_cover(db, user_id=uid, pair_id=pid,
                foreign_amount=None, cover_all=True, max_gold_in=D('200'),
                idempotency_key='interest-cover')

    async with asyncio.timeout(10):
        sweep_task = asyncio.create_task(loan_sweep.run_sweep_once())
        await scheduler_has_user.wait()
        cover_task = asyncio.create_task(cover())
        touched, execution = await asyncio.gather(sweep_task, cover_task)
    assert touched == 1
    assert execution.output_amount == D('101')
    after = await state(pg_sessionmaker, uid, pid)
    assert after['cash'] == before['cash'] - execution.input_amount
    assert after['gold'] == before['gold'] and after['foreign'] == before['foreign']
    assert after['treasury_foreign'] == before['treasury_foreign'] + D('101')
    assert after['principal'] == after['interest'] == after['locked'] == 0
    assert after['debt'] == 0 and after['version'] == before['version'] + 2
    assert [t[1] for t in after['trades']] == ['short_open', 'short_cover']
    async with pg_sessionmaker() as db:
        accruals = (await db.execute(select(AuditEvent).where(
            AuditEvent.event_type == 'interest_accrual'))).scalars().all()
        assert len(accruals) == 1
        assert accruals[0].payload['currency'] == 'foreign'
        assert D(str(accruals[0].payload['interest_foreign_delta'])) == D('1')
        assert accruals[0].payload['source'] == 'scheduler'
    assert await loan_sweep.run_sweep_once() == 0
    assert (await cover()).replay
    assert await state(pg_sessionmaker, uid, pid) == after


async def test_consumption_waiting_on_opening_user_cannot_spend_new_short_proceeds(
        pg_sessionmaker, pg_engine, monkeypatch):
    """A no-debt consumption snapshot must not bypass a newly committed short."""
    uid, pid = await seed(pg_sessionmaker, cash='100')
    before = await state(pg_sessionmaker, uid, pid)
    opener_has_user, spender_wants_user = asyncio.Event(), asyncio.Event()
    original_user_lock = shorts.lock_user

    async def hold_opening_user(db, user_id):
        user = await original_user_lock(db, user_id)
        opener_has_user.set()
        await spender_wants_user.wait()
        return user

    class SpendingSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            descriptions = getattr(statement, 'column_descriptions', [])
            if (getattr(statement, '_for_update_arg', None) is not None
                    and descriptions and descriptions[0].get('entity') is User):
                # Dependency discovery already saw no debt/short. The real
                # FOR UPDATE below must serialize against the short opener.
                spender_wants_user.set()
            return await super().execute(statement, *args, **kwargs)

    monkeypatch.setattr(shorts, 'lock_user', hold_opening_user)

    async def opening():
        async with pg_sessionmaker() as db:
            return await open_short(db, uid, pid, 'opening-before-spend')

    async def spending():
        async with SpendingSession(pg_engine, expire_on_commit=False) as db:
            try:
                result = await danmuku.exchange(db, user_id=uid,
                    qq_user_id='race-user', room_id='race-room',
                    yuan=D('150'), huo=D('0'))
                await db.commit()
                return result
            except danmuku.ExchangeError as exc:
                await db.rollback()
                return exc

    async with asyncio.timeout(10):
        opening_task = asyncio.create_task(opening())
        await opener_has_user.wait()
        spending_task = asyncio.create_task(spending())
        opened, rejected = await asyncio.gather(opening_task, spending_task)
    assert isinstance(rejected, danmuku.ExchangeError), rejected
    assert rejected.code == 'OUTSTANDING_DEBT'
    after = await state(pg_sessionmaker, uid, pid)
    assert before['cash'] < D('150') < after['cash']
    assert after['cash'] == before['cash'] + opened.output_amount
    assert after['locked'] == opened.output_amount
    assert after['cash'] - after['locked'] == before['cash']
    assert after['principal'] == D('100') and after['debt'] == 0
    assert after['gold'] == before['gold'] and after['foreign'] == before['foreign']
    assert after['version'] == before['version'] + 1
    assert [t[1] for t in after['trades']] == ['short_open']
    assert [a[1] for a in after['audits']] == ['fx_trade']
    async with pg_sessionmaker() as db:
        assert not (await db.execute(select(DanmukuExchange))).scalars().all()


async def test_new_gold_borrow_rechecks_shared_risk_after_concurrent_short_open(
        pg_sessionmaker, monkeypatch):
    """A formerly affordable loan must include a short committed while waiting."""
    uid, pid = await seed(pg_sessionmaker, cash='100')
    before = await state(pg_sessionmaker, uid, pid)
    async with pg_sessionmaker() as db:
        old_quota = await loan.get_quota(user=await db.get(User, uid), db=db)
        assert old_quota.max_borrow >= D('250')
    opener_has_user, borrower_wants_user = asyncio.Event(), asyncio.Event()
    original_open_lock, original_borrow_lock = shorts.lock_user, loan.lock_user

    async def hold_opening_user(db, user_id):
        user = await original_open_lock(db, user_id)
        opener_has_user.set()
        await borrower_wants_user.wait()
        return user

    async def observe_borrowing_user(db, user_id):
        borrower_wants_user.set()
        return await original_borrow_lock(db, user_id)

    monkeypatch.setattr(shorts, 'lock_user', hold_opening_user)
    monkeypatch.setattr(loan, 'lock_user', observe_borrowing_user)

    async def opening():
        async with pg_sessionmaker() as db:
            return await open_short(db, uid, pid, 'before-new-loan')

    async def borrowing():
        async with pg_sessionmaker() as db:
            try:
                return await loan.borrow(BorrowRequest(amount=D('250')),
                    user=await db.get(User, uid), db=db)
            except HTTPException as exc:
                return exc

    async with asyncio.timeout(10):
        opening_task = asyncio.create_task(opening())
        await opener_has_user.wait()
        borrowing_task = asyncio.create_task(borrowing())
        opened, rejected = await asyncio.gather(opening_task, borrowing_task)
    assert isinstance(rejected, HTTPException) and rejected.status_code == 400, rejected
    assert rejected.detail == 'insufficient_initial_margin'
    after = await state(pg_sessionmaker, uid, pid)
    assert after['debt'] == 0 and after['principal'] == D('100')
    assert after['cash'] == before['cash'] + opened.output_amount
    assert after['locked'] == opened.output_amount
    assert after['cash'] - after['locked'] == before['cash']
    assert after['gold'] == before['gold'] and after['foreign'] == before['foreign']
    assert after['version'] == before['version'] + 1
    assert [t[1] for t in after['trades']] == ['short_open']
    assert [a[1] for a in after['audits']] == ['fx_trade']
    async with pg_sessionmaker() as db:
        new_quota = await loan.get_quota(user=await db.get(User, uid), db=db)
        assert D('0') <= new_quota.max_borrow < D('250')
        assert new_quota.equity_to_risk_basis >= float(new_quota.r_initial)


async def test_cover_requotes_after_concurrent_player_buy_changes_pool(
        pg_sessionmaker, monkeypatch):
    """A cover discovered before a real player buy uses its committed reserves."""
    uid, pid = await seed(pg_sessionmaker)
    async with pg_sessionmaker() as db:
        await open_short(db, uid, pid, 'before-buy')
        pair = await db.get(FxPair, pid)
        old_quote = quote_buy_exact_out(D('100'), pair.gold_reserve,
            pair.foreign_reserve, pair.buy_fee_rate)
        buyer = User(username='concurrent-buyer', cash=D('1000'),
                     tos_accepted_at=datetime.now(timezone.utc))
        db.add(buyer)
        await db.commit()
        buyer_id = buyer.id
    before = await state(pg_sessionmaker, uid, pid)
    cover_discovered, buy_committed = asyncio.Event(), asyncio.Event()
    original_hold = GATES.hold

    @asynccontextmanager
    async def delay_cover_gate(*args, **kwargs):
        if asyncio.current_task().get_name() == 'pre-buy-cover':
            cover_discovered.set()
            await buy_committed.wait()
        async with original_hold(*args, **kwargs):
            yield

    monkeypatch.setattr(GATES, 'hold', delay_cover_gate)

    async def covering():
        async with pg_sessionmaker() as db:
            return await shorts.execute_short_cover(db, user_id=uid, pair_id=pid,
                foreign_amount=None, cover_all=True, max_gold_in=D('200'),
                idempotency_key='after-buy-cover')

    async def buying():
        await cover_discovered.wait()
        async with pg_sessionmaker() as db:
            result = await trading.execute_trade(db, buyer_id, pid, 'buy', D('20'), D('0'), 'concurrent-buy')
        buy_committed.set()
        return result

    async with asyncio.timeout(10):
        cover_task = asyncio.create_task(covering(), name='pre-buy-cover')
        buy_task = asyncio.create_task(buying())
        covered, buy_result = await asyncio.gather(cover_task, buy_task)
    assert buy_result.output_amount > 0
    after = await state(pg_sessionmaker, uid, pid)
    assert after['pool_version'] == before['pool_version'] + 2
    # state() totals only the short owner; account for the other buyer's wallet/cash.
    assert after['gold'] == before['gold'] + buy_result.input_amount
    assert after['foreign'] == before['foreign'] - buy_result.output_amount
    assert after['cash'] == before['cash'] - covered.input_amount
    assert after['principal'] == after['interest'] == after['locked'] == after['debt'] == 0
    assert covered.output_amount == D('100')
    assert covered.input_amount > old_quote.input_amount
    assert after['version'] == before['version'] + 1
    assert [t[1] for t in after['trades']] == ['short_open', 'spot', 'short_cover']
    assert sum(a[1] == 'fx_trade' for a in after['audits']) == 3
    async with pg_sessionmaker() as db:
        trades = (await db.execute(select(FxTrade).order_by(FxTrade.id))).scalars().all()
        buy_trade, cover_trade = trades[1:]
        assert buy_trade.source == 'player' and buy_trade.side == 'buy'
        assert buy_trade.user_id == buyer_id
        assert cover_trade.pre_gold_reserve == buy_trade.post_gold_reserve
        assert cover_trade.pre_foreign_reserve == buy_trade.post_foreign_reserve
        new_quote = quote_buy_exact_out(D('100'), buy_trade.post_gold_reserve,
            buy_trade.post_foreign_reserve, D('.01'))
        assert covered.input_amount == new_quote.input_amount
        assert after['treasury_foreign'] == (before['treasury_foreign']
            + D('100'))
