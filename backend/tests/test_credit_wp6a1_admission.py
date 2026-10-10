from datetime import datetime, timezone
from decimal import Decimal as D
import pytest
from sqlalchemy import select
from app.core.database import async_session_maker
from app.models.base import User, SiteConfig
from app.models.fx import FxPair, FxTreasury, FxWallet, FxTrade
from app.services.credit import flags
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.gates import GATES
from app.services import loan_service, site_config
from app.services.fx import trading
from app.api.v1.loan import get_quota, borrow
from app.schemas.loan import BorrowRequest
from fastapi import HTTPException
pytestmark = pytest.mark.asyncio
@pytest.fixture(autouse=True)
def unified(monkeypatch):
    flags.set_flags(flags.CreditFlags(unified_credit_enabled=True, credit_leverage=D('4'), credit_maintenance_ratio=D('.1')))
    monkeypatch.setattr(OWNERSHIP, '_writes_enabled', True)
    yield
    flags.clear_flags()
async def seed(db, cash='100', debt='50'):
    u = User(username='credit-fx', cash=D(cash), debt=D(debt), tos_accepted_at=datetime.now(timezone.utc))
    p = FxPair(currency_code='MORA', currency_name='Mora', status='trading', gold_reserve=D('10000'), foreign_reserve=D('50000'), buy_fee_rate=D('.01'), sell_fee_rate=D('.01'))
    db.add_all([u,p]); await db.flush()
    db.add(FxTreasury(pair_id=p.id))
    for key, val in [('fx_enabled','true'),('loan_enabled','true'),('loan_daily_rate','0'),('loan_leverage_k','3')]:
        c=(await db.execute(select(SiteConfig).where(SiteConfig.key==key))).scalar_one_or_none()
        if c: c.value=val
        else: db.add(SiteConfig(key=key,value=val,value_type='string'))
    await db.commit(); site_config.clear_cache()
    return u.id,p.id
async def test_debtor_can_buy_fx_with_post_trade_margin():
    async with async_session_maker() as db:
        uid,pid=await seed(db)
        result=await trading.execute_trade(db,uid,pid,'buy',D('10'),D('0'),'allowed')
        u=await db.get(User,uid)
        assert result.output_amount>0 and u.economic_version==1
        assert not GATES.held_keys()
async def test_fees_can_reject_buyer_at_initial_boundary():
    async with async_session_maker() as db:
        uid,pid=await seed(db,cash='80',debt='60')
        with pytest.raises(HTTPException) as exc:
            await trading.execute_trade(db,uid,pid,'buy',D('10'),D('0'),'denied')
        assert exc.value.status_code==400
        assert (await db.execute(select(FxTrade))).scalars().all()==[]
        assert not GATES.held_keys()
async def test_quota_counts_fx_actual_liquidation_value():
    async with async_session_maker() as db:
        uid,pid=await seed(db,cash='0',debt='0')
        db.add(FxWallet(user_id=uid,pair_id=pid,foreign_amount=D('1000')))
        await db.commit()
        result=await get_quota(user=await db.get(User,uid),db=db)
        assert D('500')<result.max_borrow<D('600')
        assert result.net_worth < D('200')
async def test_borrow_uses_fx_collateral_and_bumps_once():
    async with async_session_maker() as db:
        uid,pid=await seed(db,cash='0',debt='0')
        db.add(FxWallet(user_id=uid,pair_id=pid,foreign_amount=D('1000')))
        await db.commit()
        result=await borrow(BorrowRequest(amount='100'),user=await db.get(User,uid),db=db)
        assert result.cash==D('100') and result.debt==D('100')
        assert (await db.get(User,uid)).economic_version==1
async def test_loan_primitives_bump_versions_once():
    async with async_session_maker() as db:
        uid,pid=await seed(db)
        u=await loan_service.increase_debt(db,uid,D('1'),grant_cash=True,daily_rate=D('0'),source='borrow')
        assert u.economic_version==1
        await loan_service.decrease_debt_locked(db,u,D('1'),consume_cash=True,daily_rate=D('0'))
        assert u.economic_version==2

async def test_borrow_new_principal_does_not_accrue_past_interest():
    from datetime import timedelta
    async with async_session_maker() as db:
        uid, pid = await seed(db, cash='100', debt='50')
        u = await db.get(User, uid)
        u.debt_last_accrued_at = datetime.now(timezone.utc) - timedelta(days=1)
        config = (await db.execute(select(SiteConfig).where(SiteConfig.key == 'loan_daily_rate'))).scalar_one()
        config.value = '.1'
        await db.commit(); site_config.clear_cache()
        result = await borrow(BorrowRequest(amount='79'), user=u, db=db)
        assert D('134') <= result.debt < D('134.001')

async def test_debt_free_fx_does_not_discover_collateral(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('debt-free buy must not discover collateral')
    monkeypatch.setattr(trading, 'discover_dependencies', forbidden)
    async with async_session_maker() as db:
        uid, pid = await seed(db, debt='0')
        await trading.execute_trade(db, uid, pid, 'buy', D('10'), D('0'), 'fast')

async def test_all_fx_collateral_gates_are_held_until_commit(monkeypatch):
    from app.services.credit.keys import GroupKey
    async with async_session_maker() as db:
        uid, pid = await seed(db)
        other = FxPair(currency_code='LMD',currency_name='LMD',status='trading',gold_reserve=D('10000'),foreign_reserve=D('50000'))
        db.add(other); await db.flush()
        oid = other.id
        db.add(FxWallet(user_id=uid,pair_id=oid,foreign_amount=D('100')))
        await db.commit()
        real_commit = db.commit
        async def checked_commit():
            assert GATES.held_keys_by_current_task() == {GroupKey('fx',pid),GroupKey('fx',oid)}
            await real_commit()
        monkeypatch.setattr(db, 'commit', checked_commit)
        await trading.execute_trade(db,uid,pid,'buy',D('10'),D('0'),'gates')

async def test_read_only_rejects_flag_off_fx_and_loan(monkeypatch):
    flags.set_flags(flags.CreditFlags(read_only_instance=True))
    monkeypatch.setattr(OWNERSHIP, '_writes_enabled', False)
    from app.services.credit.ownership import EconomicWritesDisabled
    async with async_session_maker() as db:
        uid,pid=await seed(db)
        with pytest.raises(EconomicWritesDisabled):
            await trading.execute_trade(db,uid,pid,'buy',D('1'),D('0'),'readonly')
        with pytest.raises(EconomicWritesDisabled):
            await borrow(BorrowRequest(amount='1'),user=await db.get(User,uid),db=db)

async def test_fx_sale_repays_proceeds_atomically():
    async with async_session_maker() as db:
        uid,pid=await seed(db)
        db.add(FxWallet(user_id=uid,pair_id=pid,foreign_amount=D('100')))
        await db.commit()
        result = await trading.execute_trade(db,uid,pid,'sell',D('100'),D('0'),'repay-sale')
        user=await db.get(User,uid)
        assert user.debt == D('50') - result.output_amount
        assert user.cash == D('100')

async def test_fx_buy_materializes_pending_interest():
    from datetime import timedelta
    async with async_session_maker() as db:
        uid,pid=await seed(db)
        u=await db.get(User,uid)
        u.debt_last_accrued_at=datetime.now(timezone.utc)-timedelta(days=1)
        cfg=(await db.execute(select(SiteConfig).where(SiteConfig.key=='loan_daily_rate'))).scalar_one()
        cfg.value='.1'
        await db.commit(); site_config.clear_cache()
        await trading.execute_trade(db,uid,pid,'buy',D('1'),D('0'),'interest')
        await db.refresh(u)
        assert D('55')<=u.debt<D('55.001')

async def test_stale_dependency_retries_are_bounded_and_release_gates(monkeypatch):
    from dataclasses import replace
    original=trading.discover_dependencies
    calls=0
    async def stale(*args,**kwargs):
        nonlocal calls
        calls+=1
        deps=await original(*args,**kwargs)
        return replace(deps,economic_version=deps.economic_version-1)
    monkeypatch.setattr(trading,'discover_dependencies',stale)
    async with async_session_maker() as db:
        uid,pid=await seed(db)
        with pytest.raises(HTTPException) as exc:
            await trading.execute_trade(db,uid,pid,'buy',D('1'),D('0'),'stale')
        assert exc.value.status_code==409
        assert calls==flags.get_flags().credit_risk_retry_limit+1
        assert not GATES.held_keys()
        assert not db.in_transaction()

async def test_fx_in_session_has_no_commit_or_publication(monkeypatch):
    from app.services.credit.keys import GroupKey
    async with async_session_maker() as db:
        uid,pid=await seed(db,debt='0')
        async with GATES.hold(exclusive=[GroupKey('fx',pid)]):
            execution=await trading.execute_trade_in_session(db,uid,pid,'buy',D('1'),D('0'),'atomic')
            assert execution.trade.id
            await db.rollback()
        assert (await db.execute(select(FxTrade))).scalars().all()==[]

async def test_borrow_rejects_pending_interest_reducing_headroom():
    from datetime import timedelta
    async with async_session_maker() as db:
        uid,pid=await seed(db)
        user=await db.get(User,uid)
        user.debt_last_accrued_at=datetime.now(timezone.utc)-timedelta(days=1)
        cfg=(await db.execute(select(SiteConfig).where(SiteConfig.key=='loan_daily_rate'))).scalar_one()
        cfg.value='.1'
        await db.commit(); site_config.clear_cache()
        # Nominal headroom is 100; accrued debt leaves only 80.
        with pytest.raises(HTTPException) as exc:
            await borrow(BorrowRequest(amount='90'),user=user,db=db)
        assert exc.value.status_code==400
        assert not GATES.held_keys()
        from app.models.ledger import LedgerEntry
        assert (await db.execute(select(LedgerEntry).where(LedgerEntry.user_id == uid))).scalars().all() == []
        user = await db.get(User, uid)
        assert user.cash == D("100") and user.debt == D("50")

async def test_debt_free_fx_select_count_matches_flag_off():
    from sqlalchemy import event
    from app.core.database import engine
    async with async_session_maker() as db:
        uid,pid=await seed(db,debt='0')
        statements=[]
        def collect(conn,cursor,statement,parameters,context,executemany):
            if statement.lstrip().upper().startswith('SELECT'):
                statements.append(statement)
        event.listen(engine.sync_engine,'before_cursor_execute',collect)
        try:
            flags.clear_flags()
            await trading.execute_trade(db,uid,pid,'buy',D('1'),D('0'),'legacy-count')
            baseline=len(statements)
            statements.clear()
            flags.set_flags(flags.CreditFlags(unified_credit_enabled=True,credit_leverage=D('4'),credit_maintenance_ratio=D('.1')))
            await trading.execute_trade(db,uid,pid,'buy',D('1'),D('0'),'unified-count')
            assert len(statements)<=baseline
            assert not any('FROM position' in sql or 'FROM outcome' in sql for sql in statements)
        finally:
            event.remove(engine.sync_engine,'before_cursor_execute',collect)

async def test_owner_lost_during_fx_user_lock_is_rejected(monkeypatch):
    from app.services.credit.ownership import EconomicWritesDisabled
    original=trading.lock_user
    async def lose_owner(*args,**kwargs):
        result=await original(*args,**kwargs)
        monkeypatch.setattr(OWNERSHIP,'_writes_enabled',False)
        return result
    monkeypatch.setattr(trading,'lock_user',lose_owner)
    async with async_session_maker() as db:
        uid,pid=await seed(db,debt='0')
        with pytest.raises(EconomicWritesDisabled):
            await trading.execute_trade(db,uid,pid,'buy',D('1'),D('0'),'lost-owner')
        assert not GATES.held_keys()
        assert (await db.execute(select(FxTrade))).scalars().all()==[]

async def test_new_principal_starts_now_when_old_interest_rounds_to_zero():
    from datetime import timedelta
    from app.models.ledger import LedgerEntry
    now = datetime.now(timezone.utc)
    async with async_session_maker() as db:
        uid, _ = await seed(db, cash='100', debt='0.000001')
        user = await db.get(User, uid)
        user.debt_last_accrued_at = now - timedelta(days=1)
        await db.commit()
        user = await loan_service.increase_debt(
            db, uid, D('100'), grant_cash=True, daily_rate=D('.1'),
            source='borrow', now=now,
        )
        assert user.debt == D('100.000001')
        assert loan_service.pending_debt(user, D('.1'), now) == user.debt
        assert user.debt_last_accrued_at == now
        await db.flush()
        entry = (await db.execute(select(LedgerEntry).where(LedgerEntry.user_id == uid))).scalar_one()
        assert entry.cash_delta == D('100')
        assert entry.debt_delta == D('100')
        assert entry.debt_after - entry.debt_delta == D('0.000001')
        assert entry.cash_after - entry.cash_delta == D('100')
        assert entry.debt_last_accrued_at_after.replace(tzinfo=timezone.utc) == now


@pytest.mark.parametrize("operation,cash,debt,effective", [
    ("borrow", "110", "60", None),
    ("repay", "95", "45", "5"),
    ("repay-all", "50", "0", "50"),
])
async def test_loan_result_does_not_require_quota_valuation(monkeypatch, operation, cash, debt, effective):
    from app.api.v1 import loan
    from app.schemas.loan import RepayRequest
    from app.models.ledger import LedgerEntry
    async def unavailable(*args, **kwargs):
        raise RuntimeError("result quota unavailable")
    monkeypatch.setattr(loan, "_unified_quota", unavailable)
    async with async_session_maker() as db:
        uid, _ = await seed(db)
        user = await db.get(User, uid)
        if operation == "borrow":
            result = await loan.borrow(BorrowRequest(amount='10'), user=user, db=db)
        elif operation == "repay":
            result = await loan.repay(RepayRequest(amount='5'), user=user, db=db)
        else:
            result = await loan.repay_all(user=user, db=db)
        assert result.cash == D(cash) and result.debt == D(debt)
        if effective is not None:
            assert result.effective == D(effective) and result.max_borrow is None
        else:
            assert result.max_borrow == D('90')
        assert not GATES.held_keys()
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        assert user.cash == D(cash) and user.debt == D(debt)
        entry = (await db.execute(select(LedgerEntry).where(LedgerEntry.user_id == uid))).scalar_one()
        delta = D('10') if operation == 'borrow' else -D(effective)
        assert entry.cash_delta == entry.debt_delta == delta
        assert entry.cash_after == user.cash and entry.debt_after == user.debt
