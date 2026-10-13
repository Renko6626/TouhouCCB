"""Net money/identity coverage using existing real database fixtures."""
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select

from app.core.database import async_session_maker
from app.models.audit import AuditEvent
from app.models.fx import FxPoolEvent, FxTrade
from app.schemas.fx import FxNetQuoteRequest
from app.services.fx.net_trading import quote_net_trade
from tests.test_fx_short_trading import _credit_flags, _seed

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('amount,direction,purposes,remaining', [
    ('40', 'long', ['spot'], '60'),
    ('100', 'flat', ['spot'], '0'),
    ('140', 'short', ['spot', 'short_open'], '0'),
])
async def test_sell_classifies_without_writing(amount, direction, purposes, remaining):
    uid, pid = await _seed(wallet_foreign='100', sell_fee='0.01')
    async with async_session_maker() as db:
        quote = await quote_net_trade(db, uid, pid, FxNetQuoteRequest(side='sell', amount=D(amount)))
        assert quote.executable
        assert quote.final_direction == direction
        assert [leg.purpose for leg in quote.legs] == purposes
        assert quote.wallet_after == D(remaining)
        assert quote.foreign_debt_after == max(D(amount) - 100, 0)
        assert sum(leg.input_amount for leg in quote.legs) == D(amount)
        assert quote.output_amount == sum(leg.output_amount for leg in quote.legs)
        if len(quote.legs) == 2:
            assert quote.legs[1].pre_gold_reserve == quote.legs[0].post_gold_reserve
        assert (await db.execute(select(func.count()).select_from(FxPoolEvent))).scalar_one() == 0
        assert (await db.execute(select(func.count()).select_from(FxTrade))).scalar_one() == 0
        assert (await db.execute(select(func.count()).select_from(AuditEvent))).scalar_one() == 0
        assert not db.new and not db.dirty


@pytest.mark.parametrize('extra,direction,purposes', [
    ('-1', 'short', ['short_cover']), ('0', 'flat', ['short_cover']),
    ('10', 'long', ['short_cover', 'spot']),
])
async def test_buy_budget_splits_at_effective_debt(extra, direction, purposes):
    from app.services.fx.amm import quote_buy_exact_out
    uid, pid = await _seed(short={'principal': '9', 'interest': '1', 'restricted': '5'}, buy_fee='0.01')
    cost = quote_buy_exact_out(D('10'), D('1000'), D('1000'), D('0.01')).input_amount
    async with async_session_maker() as db:
        quote = await quote_net_trade(db, uid, pid, FxNetQuoteRequest(side='buy', amount=cost + D(extra)))
        assert quote.executable
        assert quote.final_direction == direction
        assert [leg.purpose for leg in quote.legs] == purposes
        if D(extra) >= 0:
            assert quote.foreign_debt_after == 0
            assert quote.legs[0].output_amount == 10
        assert not db.new and not db.dirty


async def test_existing_both_directions_requires_manual_review():
    from fastapi import HTTPException
    uid, pid = await _seed(wallet_foreign='1', short={'principal': '1'})
    async with async_session_maker() as db:
        with pytest.raises(HTTPException, match='manual review'):
            await quote_net_trade(db, uid, pid, FxNetQuoteRequest(side='sell', amount=D('1')))


async def _net(uid, pid, side, amount, *, key='net-test', borrowed='0', min_out='0', version=None):
    from app.schemas.fx import FxNetTradeRequest
    from app.services.fx.net_trading import execute_net_trade
    async with async_session_maker() as db:
        if version is None:
            q = await quote_net_trade(db, uid, pid, FxNetQuoteRequest(side=side, amount=D(amount), borrow_amount=D(borrowed)))
            version = q.economic_version
        return await execute_net_trade(db, uid, pid, FxNetTradeRequest(side=side,
            amount=D(amount), borrow_amount=D(borrowed), min_out=D(min_out),
            expected_economic_version=version, idempotency_key=key, allow_growth=True))


async def test_cross_zero_sell_receipt_replay_and_money():
    from app.models.base import User
    from app.models.fx import FxExecution, FxShortPosition, FxWallet, FxTreasury, FxPair
    uid, pid = await _seed(wallet_foreign='100', debt='50', sell_fee='0.01')
    result = await _net(uid, pid, 'sell', '140')
    assert len(result.trade_ids) == 2
    assert result.repaid_gold == 50
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        wallet = (await db.execute(select(FxWallet).where(FxWallet.user_id == uid))).scalar_one()
        short = (await db.execute(select(FxShortPosition).where(FxShortPosition.user_id == uid))).scalar_one()
        treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pid))).scalar_one()
        pair = await db.get(FxPair, pid)
        assert wallet.foreign_amount == 0 and user.debt == 0
        assert short.principal_foreign == 40
        assert short.restricted_gold == result.legs[1].output_amount
        assert user.cash == D('1000') + result.output_amount - 50
        assert treasury.foreign_balance == D('100000') - 40 + result.fee_amount
        assert pair.gold_reserve == result.legs[-1].post_gold_reserve
        trades = list((await db.execute(select(FxTrade).order_by(FxTrade.id))).scalars())
        events = list((await db.execute(select(FxPoolEvent).order_by(FxPoolEvent.id))).scalars())
        assert [e.trade_id for e in events] == result.trade_ids
        assert len(events) == 2 and all(e.kind == "trade" for e in events)
        assert events[0].post_gold_reserve == events[1].pre_gold_reserve
        assert events[0].post_foreign_reserve == events[1].pre_foreign_reserve
        assert [e.gold_volume for e in events] == [t.output_amount for t in trades]
        assert [t.execution_leg for t in trades] == [1, 2]
        assert all(t.execution_id == result.execution_id and t.idempotency_key is None for t in trades)
        assert (await db.execute(select(func.count()).select_from(FxExecution))).scalar_one() == 1
    replay = await _net(uid, pid, 'sell', '140', version=0)
    assert replay.replay and replay.trade_ids == result.trade_ids
    async with async_session_maker() as db:
        assert (await db.execute(select(func.count()).select_from(FxPoolEvent))).scalar_one() == 2


async def test_cross_zero_buy_repay_interest_and_finance_only_long():
    from app.models.base import User
    from app.models.fx import FxShortPosition, FxWallet
    uid, pid = await _seed(short={'principal': '9', 'interest': '1', 'restricted': '5'}, buy_fee='0.01')
    result = await _net(uid, pid, 'buy', '30', borrowed='10')
    assert [l.purpose for l in result.legs] == ['short_cover', 'borrow_buy']
    assert result.foreign_debt_after == 0
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        short = (await db.execute(select(FxShortPosition).where(FxShortPosition.user_id == uid))).scalar_one()
        wallet = (await db.execute(select(FxWallet).where(FxWallet.user_id == uid))).scalar_one()
        assert user.cash == 980 and user.debt == 10
        assert short.principal_foreign == short.interest_foreign == short.restricted_gold == 0
        assert wallet.foreign_amount == result.legs[1].output_amount
        assert wallet.cost_basis == result.legs[1].input_amount


@pytest.mark.parametrize('options,side,amount,borrowed,reason', [
    ({'wallet_foreign':'100', 'treasury_foreign':'1'}, 'sell', '140', '0', 'treasury'),
    ({'wallet_foreign':'100', 'cash':'0'}, 'sell', '1000', '0', 'initial_margin'),
    ({'short':{'principal':'10'}, 'cash':'1'}, 'buy', '30', '20', 'cash to cover'),
    ({'short':{'principal':'10'}, 'buy_fee':'0.01'}, 'buy', '10.203043', '0', 'fee leaves'),
])
async def test_second_leg_failures_leave_no_money_or_receipt(options, side, amount, borrowed, reason):
    from fastapi import HTTPException
    from app.models.base import User
    from app.models.fx import FxExecution, FxPair, FxWallet, FxShortPosition
    uid, pid = await _seed(**options)
    async with async_session_maker() as db:
        before_user = (await db.get(User, uid)).model_dump()
        before_pair = (await db.get(FxPair, pid)).model_dump()
    with pytest.raises((HTTPException, ValueError)) as failure:
        await _net(uid, pid, side, amount, borrowed=borrowed)
    assert reason in str(failure.value)
    async with async_session_maker() as db:
        assert (await db.get(User, uid)).model_dump() == before_user
        assert (await db.get(FxPair, pid)).model_dump() == before_pair
        assert (await db.execute(select(func.count()).select_from(FxExecution))).scalar_one() == 0
        assert (await db.execute(select(func.count()).select_from(FxPoolEvent))).scalar_one() == 0
        assert (await db.execute(select(func.count()).select_from(FxTrade))).scalar_one() == 0
        assert (await db.execute(select(func.count()).select_from(AuditEvent))).scalar_one() == 0


async def test_reduction_survives_frozen_opening_disabled_but_cross_rejected():
    from fastapi import HTTPException
    uid, pid = await _seed(short={'principal':'10'}, credit_frozen=True,
                          loan_enabled=False, short_enabled=False, reduce_only=True)
    reduced = await _net(uid, pid, 'buy', '5')
    assert reduced.final_direction == 'short'
    with pytest.raises(HTTPException, match='reduce-only'):
        await _net(uid, pid, 'buy', '20', key='cross')


async def test_quoted_close_version_change_does_not_open_short():
    from fastapi import HTTPException
    uid, pid = await _seed(wallet_foreign='100')
    first = await _net(uid, pid, 'sell', '100')
    with pytest.raises(HTTPException, match='economic_version_changed'):
        await _net(uid, pid, 'sell', '100', key='stale-close', version=first.economic_version)


async def test_key_namespace_conflicts_between_net_and_legacy():
    from fastapi import HTTPException
    from app.services.fx.trading import execute_trade
    uid, pid = await _seed()
    await _net(uid, pid, 'buy', '10')
    async with async_session_maker() as db:
        with pytest.raises(HTTPException, match='parameter mismatch'):
            await execute_trade(db, uid, pid, 'buy', D('10'), D('0'), 'net-test')
        await execute_trade(db, uid, pid, 'buy', D('10'), D('0'), 'legacy-key')
    with pytest.raises(HTTPException, match='parameter mismatch'):
        await _net(uid, pid, 'buy', '10', key='legacy-key')


async def test_net_two_leg_http_auth_strings_private_history_and_market_volume():
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from app.api.v1.fx import router
    from app.core.database import get_async_session
    from app.core.users import current_active_user
    from app.models.base import User
    from app.services.fx.candles import compute_fx_candle_rows
    uid, pid = await _seed(wallet_foreign='100', sell_fee='0.01')
    app = FastAPI()
    app.include_router(router, prefix='/fx')
    async def session():
        async with async_session_maker() as db:
            yield db
    app.dependency_overrides[get_async_session] = session
    async with AsyncClient(transport=ASGITransport(app), base_url='http://test') as client:
        unauth = await client.post(f'/fx/pairs/{pid}/net/quote', json={'side':'sell','amount':'140'})
        assert unauth.status_code == 401
        async with async_session_maker() as db:
            user = await db.get(User, uid)
        app.dependency_overrides[current_active_user] = lambda: user
        q = (await client.post(f'/fx/pairs/{pid}/net/quote', json={'side':'sell','amount':'140'})).json()
        response = await client.post(f'/fx/pairs/{pid}/net/trades', json={
            'side':'sell', 'amount':'140', 'min_out':'0', 'idempotency_key':'http-net',
            'expected_economic_version':q['economic_version'], 'allow_growth':True})
        assert response.status_code == 200, response.text
        result = response.json()
        assert isinstance(result['output_amount'], str)
        assert len(result['trade_ids']) == 2
        private = (await client.get('/fx/my-trades')).json()
        assert all(t['execution_id'] == result['execution_id'] for t in private)
        public = (await client.get(f'/fx/pairs/{pid}/trades')).json()
        assert all('execution_id' not in t and 'borrow_amount' not in t for t in public)
        # Replay leaves the source ledger (hence all derived candles) unchanged.
        replay = await client.post(f'/fx/pairs/{pid}/net/trades', json={
            'side':'sell','amount':'140','min_out':'0','idempotency_key':'http-net',
            'expected_economic_version':q['economic_version'], 'allow_growth':True})
        assert replay.json()['replay']
    async with async_session_maker() as db:
        from app.models.fx import FxPoolEvent
        rows = list((await db.execute(select(FxPoolEvent).where(
            FxPoolEvent.pair_id == pid).order_by(FxPoolEvent.id))).scalars())
        candles = compute_fx_candle_rows(rows)
        one_min = [c for c in candles if c['interval'] == '1m']
        assert sum(c['n_trades'] for c in one_min) == 2
        assert sum(c['gold_volume'] for c in one_min) == D(result['output_amount'])


async def test_pool_move_cannot_turn_confirmed_cover_into_long():
    from datetime import datetime, timezone
    from fastapi import HTTPException
    from app.models.base import User
    from app.models.fx import FxWallet
    from app.schemas.fx import FxNetTradeRequest
    from app.services.fx.net_trading import execute_net_trade
    from app.services.fx.amm import quote_buy_exact_out
    from app.services.fx.trading import execute_trade
    uid, pid = await _seed(short={'principal':'10'})
    budget = quote_buy_exact_out(D('10'), D('1000'), D('1000'), D('0')).input_amount
    async with async_session_maker() as db:
        q = await quote_net_trade(db, uid, pid, FxNetQuoteRequest(side='buy', amount=budget))
        assert q.final_direction == 'flat' and q.growth_amount == 0
        seller = User(username='other-seller', cash=D('1000'), tos_accepted_at=datetime.now(timezone.utc))
        db.add(seller); await db.flush()
        db.add(FxWallet(user_id=seller.id, pair_id=pid, foreign_amount=D('100')))
        await db.commit()
        await execute_trade(db, seller.id, pid, 'sell', D('100'), D('0'), 'pool-move')
        with pytest.raises(HTTPException, match='growth_requires_confirmation'):
            await execute_net_trade(db, uid, pid, FxNetTradeRequest(side='buy', amount=budget,
                min_out=q.output_amount, expected_economic_version=q.economic_version,
                idempotency_key='quoted-cover', allow_growth=False))
    async with async_session_maker() as db:
        q2 = await quote_net_trade(db, uid, pid, FxNetQuoteRequest(side='buy', amount=budget))
        assert q2.economic_version == q.economic_version and q2.growth_amount > 0
        assert (await db.execute(select(func.count()).select_from(FxTrade))).scalar_one() == 1


async def test_financing_intermediate_credit_is_storage_bounded():
    from fastapi import HTTPException
    from app.models.ledger import LedgerEntry
    uid, pid = await _seed(cash='9999999998.999999')
    async with async_session_maker() as db:
        q = await quote_net_trade(db, uid, pid, FxNetQuoteRequest(side='buy', amount=D('3'), borrow_amount=D('2')))
        assert not q.executable and q.blocked_reason == 'trade exceeds storage limit'
    with pytest.raises(HTTPException, match='storage limit'):
        await _net(uid, pid, 'buy', '3', borrowed='2')
    async with async_session_maker() as db:
        assert (await db.execute(select(func.count()).select_from(LedgerEntry))).scalar_one() == 0


async def test_cross_sale_can_borrow_spot_fee_but_not_future_short_fee():
    from app.models.fx import FxTreasury
    uid, pid = await _seed(wallet_foreign='100', treasury_foreign='39.5', sell_fee='0.01')
    result = await _net(uid, pid, 'sell', '140')
    assert result.final_direction == 'short'
    async with async_session_maker() as db:
        treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pid))).scalar_one()
        assert treasury.foreign_balance == D('0.9')


async def test_later_ledger_error_rolls_back_already_written_first_leg(monkeypatch):
    from app.models.base import User
    from app.models.fx import FxExecution, FxPair, FxWallet, FxTreasury
    from app.services.fx import shorts
    uid, pid = await _seed(wallet_foreign='100', debt='50', sell_fee='0.01')
    async with async_session_maker() as db:
        before_user = (await db.get(User, uid)).model_dump()
        before_pair = (await db.get(FxPair, pid)).model_dump()
        before_treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pid))).scalar_one().model_dump()
    original = shorts._write_open_ledger
    async def fail_after_real_second_ledger(*args, **kwargs):
        result = await original(*args, **kwargs)
        assert result.trade.id is not None  # Real pool/cash/loan/legs were flushed.
        raise RuntimeError('simulated storage failure after second leg')
    monkeypatch.setattr(shorts, '_write_open_ledger', fail_after_real_second_ledger)
    with pytest.raises(RuntimeError, match='storage failure'):
        await _net(uid, pid, 'sell', '140')
    async with async_session_maker() as db:
        assert (await db.get(User, uid)).model_dump() == before_user
        assert (await db.get(FxPair, pid)).model_dump() == before_pair
        assert (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pid))).scalar_one().model_dump() == before_treasury
        assert (await db.execute(select(FxWallet.foreign_amount).where(FxWallet.user_id == uid))).scalar_one() == 100
        for model in (FxPoolEvent, FxTrade, FxExecution, AuditEvent):
            assert (await db.execute(select(func.count()).select_from(model))).scalar_one() == 0


async def test_net_uses_one_interest_timestamp_and_keeps_audit_replay(monkeypatch):
    from datetime import timedelta
    from app.models.fx import FxShortPosition
    from app.services.fx import trading
    from app.services import audit_replay
    from app.services.fx import shorts
    from tests.test_fx_short_trading import _open
    uid, pid = await _seed(rate='0.1')
    initial = trading.utcnow()
    monkeypatch.setattr(shorts, 'utcnow', lambda: initial)
    await _open(uid, pid, '10')
    fixed = initial + timedelta(days=1)
    monkeypatch.setattr(trading, 'utcnow', lambda: fixed)
    async with async_session_maker() as db:
        q = await quote_net_trade(db, uid, pid, FxNetQuoteRequest(side='buy', amount=D('30')))
        assert q.legs[0].output_amount == 11
        assert not db.dirty
    result = await _net(uid, pid, 'buy', '30')
    assert result.legs[0].output_amount == 11 and result.foreign_debt_after == 0
    async with async_session_maker() as db:
        events = await audit_replay.load_events(db)
        snap, mismatches = audit_replay.fold(events, check=True)
        assert mismatches == []
        assert await audit_replay.compare_with_live(db, snap) == []


async def test_net_cover_cannot_spend_sibling_locks_or_new_loan():
    from app.models.fx import FxPair, FxTreasury, FxShortPosition
    from fastapi import HTTPException
    from tests.test_fx_short_trading import _short_row
    uid, pid = await _seed(cash='910', short={'principal':'10'})
    async with async_session_maker() as db:
        other = FxPair(currency_code='LOCKED', currency_name='Locked', status='trading',
                       gold_reserve=D('1000'), foreign_reserve=D('1000'))
        db.add(other); await db.flush()
        db.add(FxTreasury(pair_id=other.id, gold_balance=D('1000'), foreign_balance=D('1000')))
        db.add(_short_row(uid, other.id, principal='8', restricted='900'))
        await db.commit()
    with pytest.raises(HTTPException, match='cash to cover'):
        await _net(uid, pid, 'buy', '20', borrowed='10')
    async with async_session_maker() as db:
        assert (await db.execute(select(FxShortPosition.restricted_gold).where(
            FxShortPosition.user_id == uid, FxShortPosition.pair_id == other.id))).scalar_one() == 900
        assert (await db.execute(select(func.count()).select_from(FxPoolEvent))).scalar_one() == 0
        assert (await db.execute(select(func.count()).select_from(FxTrade))).scalar_one() == 0


async def test_spot_sale_first_credit_before_repay_is_storage_bounded():
    from app.models.base import User
    from app.models.ledger import LedgerEntry
    from fastapi import HTTPException
    uid, pid = await _seed(cash='9999999998.999999', debt='100', wallet_foreign='10', sell_fee='0.01')
    async with async_session_maker() as db:
        before = (await db.get(User, uid)).model_dump()
        q = await quote_net_trade(db, uid, pid, FxNetQuoteRequest(side='sell', amount=D('10')))
        assert not q.executable and q.blocked_reason == 'trade exceeds storage limit'
    with pytest.raises(HTTPException, match='storage limit'):
        await _net(uid, pid, 'sell', '10')
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        assert user.model_dump() == before
        for model in (FxTrade, AuditEvent, LedgerEntry):
            assert (await db.execute(select(func.count()).select_from(model))).scalar_one() == 0


async def test_private_history_completes_cross_zero_at_row_limit():
    from app.api.v1.fx import all_my_trades, list_my_trades
    from app.models.base import User
    from app.services.fx.trading import execute_trade
    uid, pid = await _seed(wallet_foreign='100')
    cross = await _net(uid, pid, 'sell', '140')
    # A different user's newer public trades must never enter private history.
    from datetime import datetime, timezone
    async with async_session_maker() as db:
        other = User(username='history-other', cash=D('1000'), tos_accepted_at=datetime.now(timezone.utc))
        db.add(other); await db.commit()
        await execute_trade(db, other.id, pid, 'buy', D('1'), D('0'), 'other-history')
    # Backwards compatible legacy rows count towards the limit independently.
    async with async_session_maker() as db:
        for index in range(49):
            source = await db.get(FxTrade, cross.trade_ids[1])
            row = FxTrade(pair_id=pid, user_id=uid, side='sell', purpose='spot',
                input_amount=D('1'), output_amount=D('1'), fee_amount=D('0'),
                pre_gold_reserve=source.post_gold_reserve, pre_foreign_reserve=source.post_foreign_reserve,
                post_gold_reserve=source.post_gold_reserve, post_foreign_reserve=source.post_foreign_reserve,
                post_price=source.post_price, idempotency_key=f'old-history-{index}')
            db.add(row)
        await db.commit()
        user = await db.get(User, uid)
        for read in (all_my_trades, list_my_trades):
            kwargs = {'user': user, 'db': db, 'limit': 50}
            if read is list_my_trades:
                kwargs['pair_id'] = pid
            history = await read(**kwargs)
            assert len(history) == 51
            legs = [t for t in history if t.execution_id == cross.execution_id]
            assert len(legs) == 2 and sum(t.input_amount for t in legs) == 140
            assert {t.id for t in legs} == set(cross.trade_ids)
            assert [t.id for t in history] == sorted((t.id for t in history), reverse=True)
        legacy_only = await list_my_trades(pid, limit=49, user=user, db=db)
        assert len(legacy_only) == 49 and all(t.execution_id is None for t in legacy_only)


@pytest.mark.parametrize('missing_treasury', [False, True])
async def test_quote_missing_associations_ignores_other_users_wallet(missing_treasury):
    from uuid import uuid4
    from app.models.base import User
    from app.models.fx import FxTreasury, FxWallet
    uid, pid = await _seed()
    async with async_session_maker() as db:
        other = User(username=uuid4().hex)
        db.add(other)
        await db.flush()
        other_wallet = FxWallet(user_id=other.id, pair_id=pid, foreign_amount=D('100'))
        db.add(other_wallet)
        if missing_treasury:
            treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pid))).scalar_one()
            await db.delete(treasury)
        await db.commit()
        before = other_wallet.model_dump()
        cash = (await db.get(User, uid)).cash
        q = await quote_net_trade(db, uid, pid, FxNetQuoteRequest(side='buy', amount=D('10')))
        assert q.executable and q.final_direction == 'long'
        assert q.wallet_after == q.output_amount
        assert other_wallet.model_dump() == before and (await db.get(User, uid)).cash == cash
        assert (await db.execute(select(FxWallet).where(FxWallet.user_id == uid))).scalar_one_or_none() is None
        assert not db.new and not db.dirty and not db.deleted
        if missing_treasury:
            assert (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pid))).scalar_one_or_none() is None


async def test_quote_missing_pair_precedes_missing_user():
    from fastapi import HTTPException
    from sqlalchemy.exc import NoResultFound
    uid, pid = await _seed()
    async with async_session_maker() as db:
        req = FxNetQuoteRequest(side='buy', amount=D('10'))
        with pytest.raises(HTTPException) as caught:
            await quote_net_trade(db, uid + 1, pid + 1, req)
        assert caught.value.status_code == 404 and caught.value.detail == 'FX pair not found'
        with pytest.raises(NoResultFound):
            await quote_net_trade(db, uid + 1, pid, req)
        assert not db.new and not db.dirty


@pytest.mark.parametrize('side,balance_field', [('buy', 'gold_balance'), ('sell', 'foreign_balance')])
async def test_quote_treasury_fee_storage_limit_keeps_balances(side, balance_field):
    from app.models.base import User
    from app.models.fx import FxPair, FxTreasury, FxWallet
    uid, pid = await _seed(wallet_foreign='100', buy_fee='0.01', sell_fee='0.01')
    async with async_session_maker() as db:
        treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pid))).scalar_one()
        setattr(treasury, balance_field, D('9999999999.999999'))
        await db.commit()
        user = await db.get(User, uid)
        pair = await db.get(FxPair, pid)
        wallet = (await db.execute(select(FxWallet).where(FxWallet.user_id == uid))).scalar_one()
        entities = (user, pair, wallet, treasury)
        before = [entity.model_dump() for entity in entities]
        q = await quote_net_trade(db, uid, pid, FxNetQuoteRequest(side=side, amount=D('10')))
        assert not q.executable and q.blocked_reason == 'trade exceeds storage limit'
        assert [entity.model_dump() for entity in entities] == before
        assert not db.new and not db.dirty


async def test_private_commit_hints_skip_replay_and_failed_trade():
    from app.services.account_events import EVENTS
    from fastapi import HTTPException
    uid, pid = await _seed(wallet_foreign='100')
    sub = EVENTS.subscribe(uid)
    try:
        result = await _net(uid, pid, 'sell', '10', key='hint-once')
        event, payload = sub.queue.get_nowait()
        from app.models.base import User
        async with async_session_maker() as db:
            committed = await db.get(User, uid)
            assert event == 'account_changed' and payload['economic_version'] == str(committed.economic_version)
        replay = await _net(uid, pid, 'sell', '10', key='hint-once', version=0)
        assert replay.replay and sub.queue.empty()
        with pytest.raises(HTTPException):
            await _net(uid, pid, 'buy', '1000000', key='hint-fail')
        assert sub.queue.empty()
    finally:
        EVENTS.unsubscribe(sub)


async def _instruction(uid, pid, instruction, *, key='instruction', rollback=False):
    from app.services.credit.gates import GATES
    from app.services.credit.keys import GroupKey
    from app.services.credit.risk import discover_dependencies
    from app.services.fx.net_trading import execute_fx_instruction_in_session
    async with async_session_maker() as db:
        target = GroupKey('fx', pid)
        deps = await discover_dependencies(db, uid, extra_groups=[target])
        await db.rollback()
        async with GATES.hold(exclusive=[target], shared=[g for g in deps.groups if g != target]):
            result = await execute_fx_instruction_in_session(db, user_id=uid, pair_id=pid,
                instruction=instruction, request_key=key, credit_deps=deps)
            if rollback:
                await db.rollback()
            else:
                await db.commit()
            return result


@pytest.mark.parametrize('limit,success', [('20', True), ('19', False)])
async def test_instruction_auto_borrow_actual_gap_and_limit(limit, success):
    from fastapi import HTTPException
    from app.models.base import User
    from app.models.fx import FxExecution, FxPair, FxWallet
    from app.schemas.fx import FxTradeInstruction
    uid, pid = await _seed(cash='80')
    instruction = FxTradeInstruction(action='buy', amount=D('100'), auto_borrow=True, borrow_limit=D(limit))
    if success:
        result = await _instruction(uid, pid, instruction)
        assert result.borrow_amount == 20
        assert result.gold_debt_delta == 20
    else:
        with pytest.raises(HTTPException, match='auto borrow limit exceeded'):
            await _instruction(uid, pid, instruction)
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        assert user.cash == (0 if success else 80)
        assert user.debt == (20 if success else 0)
        if not success:
            assert (await db.get(FxPair, pid)).gold_reserve == 1000
            for model in (FxTrade, FxExecution, FxWallet, AuditEvent):
                assert (await db.execute(select(func.count()).select_from(model))).scalar_one() == 0


async def test_instruction_cross_zero_finances_only_new_long_and_rollback():
    from app.models.base import User
    from app.models.fx import FxExecution, FxPair, FxShortPosition, FxWallet
    from app.schemas.fx import FxTradeInstruction
    uid, pid = await _seed(cash='20', short={'principal': '10', 'restricted': '20'})
    result = await _instruction(uid, pid,
        FxTradeInstruction(action='buy', amount=D('30'), auto_borrow=True, borrow_limit=D('10')),
        rollback=True)
    assert result.borrow_amount == 10
    assert [leg.purpose for leg in result.legs] == ['short_cover', 'borrow_buy']
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        assert user.cash == 20 and user.debt == 0
        pair = await db.get(FxPair, pid)
        assert pair.gold_reserve == 1000 and pair.foreign_reserve == 1000
        position = (await db.execute(select(FxShortPosition))).scalar_one()
        assert position.principal_foreign == 10 and position.restricted_gold == 20
        for model in (FxTrade, FxExecution, FxWallet, FxPoolEvent, AuditEvent):
            assert (await db.execute(select(func.count()).select_from(model))).scalar_one() == 0


@pytest.mark.parametrize('action,percent,remaining', [('close_long','50','5'), ('close_short','50','5'), ('close_short','100','0')])
async def test_instruction_closes_current_position_and_replays(action, percent, remaining):
    from app.models.fx import FxShortPosition
    from app.schemas.fx import FxTradeInstruction
    uid, pid = await _seed(wallet_foreign='10' if action == 'close_long' else None,
        short={'principal':'9','interest':'1','restricted':'20'} if action == 'close_short' else None)
    instruction = FxTradeInstruction(action=action, close_percent=D(percent))
    result = await _instruction(uid, pid, instruction)
    replay = await _instruction(uid, pid, instruction)
    assert replay.replay
    if action == 'close_long':
        assert result.wallet_after == D(remaining)
        assert result.growth_amount == 0
    else:
        async with async_session_maker() as db:
            position = (await db.execute(select(FxShortPosition))).scalar_one()
            assert position.principal_foreign + position.interest_foreign == D(remaining)
