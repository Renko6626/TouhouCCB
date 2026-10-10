"""Financed FX buys must commit both legs or neither, and replay one identity."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.database import async_session_maker
from app.models.audit import AuditEvent
from app.models.base import SiteConfig, User
from app.models.fx import FxPair, FxShortPosition, FxTrade, FxTreasury, FxWallet
from app.models.ledger import LedgerEntry
from app.services import site_config
from app.services.credit import flags
from app.services.credit.flags import CreditFlags
from app.services.fx import borrow_buy, trading

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def credit(monkeypatch):
    flags.set_flags(CreditFlags(credit_leverage=D('4'),
                               credit_maintenance_ratio=D('0.1')))
    monkeypatch.setattr(trading, 'utcnow', lambda: NOW)
    yield
    flags.clear_flags()
    flags.set_new_risk_frozen(None)


async def seed(*, cash='100', debt='0', rate='0'):
    async with async_session_maker() as s:
        s.add_all([SiteConfig(key='fx_enabled', value='true', value_type='bool'),
                   SiteConfig(key='loan_enabled', value='true', value_type='bool'),
                   SiteConfig(key='loan_daily_rate', value=rate, value_type='decimal')])
        u = User(username='finance', cash=D(cash), debt=D(debt), tos_accepted_at=NOW,
                 debt_last_accrued_at=NOW - timedelta(days=2))
        p = FxPair(currency_code='FIN', currency_name='Finance', status='trading',
                   gold_reserve=D('10000'), foreign_reserve=D('10000'),
                   buy_fee_rate=D('0.01'), sell_fee_rate=D('0.01'))
        s.add_all([u, p])
        await s.flush()
        s.add(FxTreasury(pair_id=p.id))
        await s.commit()
        return u.id, p.id


async def state(uid, pid):
    async with async_session_maker() as s:
        u, p = await s.get(User, uid), await s.get(FxPair, pid)
        w = (await s.execute(select(FxWallet.foreign_amount).where(
            FxWallet.user_id == uid, FxWallet.pair_id == pid))).scalar_one_or_none()
        counts = [await s.scalar(select(func.count()).select_from(m))
                  for m in (FxTrade, LedgerEntry, AuditEvent)]
        return (u.cash, u.debt, p.gold_reserve, p.foreign_reserve, w, *counts)


async def execute(uid, pid, *, amount='50', borrow='20', minimum='0', key='fin-1'):
    async with async_session_maker() as s:
        return await borrow_buy.execute_borrow_buy(s, uid, pid, D(amount), D(borrow),
                                                   D(minimum), key)


async def test_success_replay_and_original_terms_survive_later_freeze():
    uid, pid = await seed(debt='10', rate='0.01')
    result = await execute(uid, pid)
    saved = await state(uid, pid)
    assert saved[0:2] == (D('70'), D('30.201000'))
    assert saved[2] == D('10049.500000')
    assert saved[4] == result.output_amount
    assert saved[5:7] == (1, 1)
    assert result.cash_amount == D('30') and result.borrow_amount == D('20')
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        u.credit_frozen = True
        await s.commit()
    replay = await execute(uid, pid)
    assert replay.trade_id == result.trade_id and replay.replay
    assert replay.output_amount == result.output_amount
    assert await state(uid, pid) == saved
    with pytest.raises(HTTPException) as exc:
        await execute(uid, pid, borrow='21')
    assert exc.value.status_code == 409
    async with async_session_maker() as s:
        with pytest.raises(HTTPException) as exc:
            await trading.execute_trade(s, uid, pid, 'buy', D('50'), D('0'), 'fin-1')
        assert exc.value.status_code == 409


@pytest.mark.parametrize('case', ['slippage', 'margin', 'cash', 'frozen', 'loan_off', 'short', 'locked'])
async def test_rejections_leave_both_legs_unchanged(case):
    uid, pid = await seed(cash='1' if case == 'margin' else '100', debt='10', rate='0.01')
    async with async_session_maker() as s:
        if case == 'frozen':
            (await s.get(User, uid)).credit_frozen = True
        elif case == 'loan_off':
            config = (await s.execute(select(SiteConfig).where(SiteConfig.key == 'loan_enabled'))).scalar_one()
            config.value = 'false'
        elif case in ('short', 'locked'):
            target = pid
            if case == 'locked':
                p = FxPair(currency_code='LOCK', currency_name='Lock', status='trading',
                           gold_reserve=D('10000'), foreign_reserve=D('10000'))
                s.add(p)
                await s.flush()
                target = p.id
            s.add(FxShortPosition(user_id=uid, pair_id=target, principal_foreign=D('1'),
                                 restricted_gold=D('90'), interest_last_accrued_at=NOW))
        await s.commit()
    site_config.clear_cache()
    before = await state(uid, pid)
    with pytest.raises(HTTPException):
        await execute(uid, pid, amount='150' if case == 'cash' else '50',
                      minimum='10000' if case == 'slippage' else '0')
    assert await state(uid, pid) == before


async def test_quote_is_read_only_and_uses_the_complete_post_buy():
    uid, pid = await seed(debt='10', rate='0.01')
    before = await state(uid, pid)
    async with async_session_maker() as s:
        q = await borrow_buy.quote_borrow_buy(s, uid, pid, D('50'), D('20'))
        await s.commit()  # A quote cannot hide writes behind a caller rollback.
    assert q.executable and q.affordable
    assert q.cash_amount == D('30') and q.estimated_debt == D('30.201000')
    assert q.estimated_equity is not None and q.estimated_risk_basis > 0
    assert q.margin_status == 'healthy'
    assert await state(uid, pid) == before
    result = await execute(uid, pid)
    assert q.output_amount == result.output_amount


async def test_quote_reports_post_trade_margin_failure():
    uid, pid = await seed(cash='1')
    async with async_session_maker() as s:
        q = await borrow_buy.quote_borrow_buy(s, uid, pid, D('20'), D('20'))
    assert not q.executable and q.blocked_reason == 'insufficient_initial_margin'
    assert q.estimated_equity is not None


async def test_different_request_keys_are_independent_buys():
    uid, pid = await seed()
    a = await execute(uid, pid)
    b = await execute(uid, pid, key='fin-2')
    assert a.trade_id != b.trade_id
    current = await state(uid, pid)
    assert current[:2] == (D('40'), D('40'))
    assert current[5:7] == (2, 2)


async def test_authenticated_http_contract_and_private_financing_history():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api.v1.fx import router
    from app.core.database import get_async_session
    from app.core.users import current_active_user
    uid, pid = await seed()
    app = FastAPI()
    app.include_router(router, prefix='/api/v1/fx')
    async def session():
        async with async_session_maker() as s:
            yield s
    async def user():
        async with async_session_maker() as s:
            return await s.get(User, uid)
    app.dependency_overrides[get_async_session] = session
    app.dependency_overrides[current_active_user] = user
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        path = f'/api/v1/fx/pairs/{pid}/borrow-buy'
        quote = await client.post(path + '/quote', json={'amount': '50', 'borrow_amount': '20'})
        assert quote.status_code == 200 and quote.json()['input_amount'] == '50'
        invalid = await client.post(path, json={'amount': '1', 'borrow_amount': '2', 'min_out': '0', 'idempotency_key': 'bad'})
        assert invalid.status_code == 422
        result = await client.post(path, json={'amount': '50', 'borrow_amount': '20', 'min_out': '0', 'idempotency_key': 'http'})
        assert result.status_code == 200
        assert isinstance(result.json()['borrow_amount'], str)
        assert D(result.json()['borrow_amount']) == D('20')
        assert D(result.json()['cash_amount']) == D('30')
        personal = await client.get(f'/api/v1/fx/pairs/{pid}/my-trades')
        assert personal.json()[0]['purpose'] == 'borrow_buy'
        assert personal.json()[0]['borrow_amount'] == '20.000000'
        public = await client.get(f'/api/v1/fx/pairs/{pid}/trades')
        assert 'borrow_amount' not in public.json()[0]
