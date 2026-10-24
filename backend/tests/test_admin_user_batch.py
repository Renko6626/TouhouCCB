"""Explicit admin closes use real settlement kernels and preserve pair atomicity."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.audit import AuditEvent
from app.models.base import Market, MarketStatus, Outcome, Position, SiteConfig, User
from app.models.credit import LiquidationAction, LiquidationRun
from app.models.fx import FxPair, FxShortPosition, FxTrade, FxTreasury, FxTriggerOrder, FxWallet
from app.services import audit_replay, site_config
from app.services.admin_user_batch import run_batch
from app.services.credit.flags import CreditFlags, set_flags
from app.services.credit.ownership import OWNERSHIP
from app.services.market_writer import WRITER
from tests.conftest import publish_seeded_market_records

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures('published_markets')]


@pytest_asyncio.fixture(autouse=True)
async def lifecycle():
    await OWNERSHIP.acquire()
    set_flags(CreditFlags(credit_leverage=D('20'), credit_maintenance_ratio=D('.04')))
    yield
    await WRITER.stop()
    set_flags(CreditFlags())


async def _seed(*, cash='1000', spot='10', principal='20', disabled=True, rate='0'):
    async with async_session_maker() as db:
        admin = User(username='admin', is_superuser=True)
        user = User(username='target', cash=D(cash), is_active=not disabled, auto_repay=False)
        pair = FxPair(currency_code='TEST', currency_name='Test', status='trading',
            gold_reserve=D('1000'), foreign_reserve=D('1000'),
            buy_fee_rate=D('.01'), sell_fee_rate=D('.02'))
        db.add_all([admin, user, pair])
        await db.flush()
        db.add_all([FxWallet(user_id=user.id, pair_id=pair.id, foreign_amount=D(spot)),
            FxTreasury(pair_id=pair.id, gold_balance=D('1000'), foreign_balance=D('100000'))])
        if D(principal):
            db.add(FxShortPosition(user_id=user.id, pair_id=pair.id,
                principal_foreign=D(principal), proceeds_basis_gold=D(principal),
                interest_last_accrued_at=datetime.now(timezone.utc) - timedelta(days=1)))
        db.add(SiteConfig(key='loan_daily_rate', value=rate, value_type='decimal'))
        db.add(SiteConfig(key='sell_fee_rate', value='.02', value_type='decimal'))
        db.add(FxTriggerOrder(user_id=user.id, pair_id=pair.id, comparison='lte',
            trigger_price=D('.1'), action='buy', amount=D('10')))
        await db.commit()
        site_config.clear_cache()
        return admin.id, user.id, pair.id


async def _batch(aid, ids, *, action='close', dry_run=False):
    async with async_session_maker() as db:
        return await run_batch(db, user_ids=ids, action=action, reason='运营处理',
            dry_run=dry_run, admin_id=aid)


async def test_disabled_spot_and_accrued_short_close():
    aid, uid, pid = await _seed(rate='.01')
    preview = await _batch(aid, [uid], dry_run=True)
    assert preview['items'][0]['status'] == 'ready'
    assert preview['items'][0]['trades'][1]['quantity'] == pytest.approx(20.2, abs=.00001)
    result = await _batch(aid, [uid])
    item = result['items'][0]
    assert item['status'] == 'success', item
    assert [t['side'] for t in item['trades']] == ['sell', 'cover']
    assert item['cash_after'] == pytest.approx(preview['items'][0]['cash_after'], abs=.0001)
    async with async_session_maker() as db:
        assert (await db.get(User, uid)).is_active is False
        assert (await db.execute(select(FxWallet.foreign_amount))).scalar_one() == 0
        short = (await db.execute(select(FxShortPosition))).scalar_one()
        assert short.principal_foreign == short.interest_foreign == 0
        assert short.interest_last_accrued_at is None
        assert (await db.execute(select(FxTriggerOrder.status))).scalar_one() == 'cancelled'
        assert [a.kind for a in (await db.execute(select(LiquidationAction).order_by(LiquidationAction.id))).scalars()] == ['sell_group', 'cover_group']
        assert (await db.execute(select(LiquidationRun.status))).scalar_one() == 'stopped'
        event = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == 'liquidation_action'))).scalar_one()
        assert event.operator_user_id == aid and event.payload['reason'] == '运营处理'
    again = await _batch(aid, [uid])
    assert again['items'][0]['status'] == 'success'
    assert again['items'][0]['trades'] == []


async def test_admin_cover_audit_replays_real_open_and_interest():
    from tests.test_fx_short_trading import _open
    aid, uid, pid = await _seed(rate='.01', spot='0', principal='0', disabled=False)
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        user.tos_accepted_at = datetime.now(timezone.utc)
        for key in ('fx_enabled', 'fx_short_enabled', 'loan_enabled'):
            db.add(SiteConfig(key=key, value='true', value_type='bool'))
        await db.commit()
    site_config.clear_cache()
    with patch('app.services.fx.shorts.utcnow', return_value=datetime.now(timezone.utc) - timedelta(days=1)):
        await _open(uid, pid, '20')
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        user.is_active = False
        await db.commit()
    await publish_seeded_market_records()
    result = await _batch(aid, [uid])
    assert result['items'][0]['status'] == 'success', result
    assert result['items'][0]['trades'][0]['quantity'] == pytest.approx(20.2, abs=.00001)
    async with async_session_maker() as db:
        snap, errors = audit_replay.fold(await audit_replay.load_events(db), check=True)
        assert errors == []
        assert await audit_replay.compare_with_live(db, snap) == []


async def test_insufficient_cover_rolls_back_sale_and_orders_and_ban_remains():
    aid, uid, pid = await _seed(cash='0', spot='1', principal='500', disabled=False)
    preview = await _batch(aid, [uid], dry_run=True)
    assert preview['items'][0]['status'] == 'failed'
    assert preview['price_impacts'] == []
    result = await _batch(aid, [uid], action='ban_and_close')
    assert result['items'][0]['status'] == 'failed'
    assert '已封禁' in result['items'][0]['message']
    assert result['items'][0]['trades'] == []
    async with async_session_maker() as db:
        user, pair = await db.get(User, uid), await db.get(FxPair, pid)
        assert user.cash == 0 and not user.is_active
        assert pair.gold_reserve == pair.foreign_reserve == D('1000')
        assert (await db.execute(select(FxWallet.foreign_amount))).scalar_one() == 1
        assert (await db.execute(select(FxTriggerOrder.status))).scalar_one() == 'waiting'
        assert list((await db.execute(select(FxTrade))).scalars()) == []
        assert list((await db.execute(select(LiquidationAction))).scalars()) == []


async def test_preview_rolls_reserves_across_selected_users_in_execution_order():
    aid, uid, pid = await _seed(principal='0', spot='100')
    async with async_session_maker() as db:
        other = User(username='other', cash=D('0'))
        db.add(other)
        await db.flush()
        db.add(FxWallet(user_id=other.id, pair_id=pid, foreign_amount=D('100')))
        await db.commit()
        oid = other.id
    preview = await _batch(aid, [uid, oid, uid], dry_run=True)
    assert len(preview['items']) == 2
    first, second = preview['items']
    assert first['trades'][0]['gold'] > second['trades'][0]['gold']
    result = await _batch(aid, [uid, oid])
    assert [i['status'] for i in result['items']] == ['success', 'success']
    assert [i['cash_after'] for i in result['items']] == pytest.approx([i['cash_after'] for i in preview['items']])
    assert result['price_impacts'][0]['price_after'] == pytest.approx(preview['price_impacts'][0]['price_after'])


async def test_batch_rejects_self_admin_disabled_bot_and_missing_and_bans_plain_user():
    aid, uid, _ = await _seed(principal='0', spot='0', disabled=False)
    async with async_session_maker() as db:
        bot = User(username='bot', is_bot=True, is_active=False)
        admin = User(username='other_admin', is_superuser=True)
        db.add_all([bot, admin])
        await db.commit()
        ids = [aid, admin.id, bot.id, 999999, uid]
    result = await _batch(aid, ids, action='ban')
    assert [i['status'] for i in result['items']] == ['failed'] * 4 + ['success']
    result = await _batch(aid, [uid], action='unban')
    assert result['items'][0]['status'] == 'success'
    async with async_session_maker() as db:
        assert (await db.get(User, uid)).is_active
        events = list((await db.execute(select(AuditEvent).where(AuditEvent.user_id == uid)
            .order_by(AuditEvent.id))).scalars())
        assert [e.event_type for e in events] == ['admin_ban', 'admin_unban']
        assert all(e.operator_user_id == aid and e.payload['reason'] == '运营处理' for e in events)


@pytest.mark.parametrize('pause_after_preview', [False, True])
async def test_lmsr_close_preserves_cash_and_reports_committed_legs(pause_after_preview):
    aid, uid, pid = await _seed(cash='0', spot='0', principal='20')
    async with async_session_maker() as db:
        user = await db.get(User, uid)
        user.debt = D('100')
        user.auto_repay = True
        user.debt_last_accrued_at = datetime.now(timezone.utc)
        market = Market(title='Forecast', liquidity_b=100, status=MarketStatus.TRADING)
        db.add(market)
        await db.flush()
        a, b = Outcome(market_id=market.id, label='yes', total_shares=D('100')), Outcome(market_id=market.id, label='no')
        db.add_all([a, b])
        await db.flush()
        db.add(Position(user_id=uid, outcome_id=a.id, amount=D('100')))
        await db.commit()
    await publish_seeded_market_records()
    await WRITER.start()
    preview = await _batch(aid, [uid], dry_run=True)
    assert preview['items'][0]['status'] == 'ready'
    if pause_after_preview:
        async with async_session_maker() as db:
            pair = await db.get(FxPair, pid)
            pair.status = 'paused'
            await db.commit()
        await publish_seeded_market_records()
    result = await _batch(aid, [uid])
    assert result['items'][0]['status'] == ('failed' if pause_after_preview else 'success'), result
    assert [t['product'] for t in result['items'][0]['trades']] == (['lmsr'] if pause_after_preview else ['lmsr', 'fx'])
    async with async_session_maker() as db:
        assert list((await db.execute(select(Position))).scalars()) == []
        assert (await db.execute(select(FxShortPosition.principal_foreign))).scalar_one() == (20 if pause_after_preview else 0)
        event = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == 'liquidation_action',
            AuditEvent.payload['product'].as_string() == 'lmsr'))).scalar_one()
        assert event.operator_user_id == aid and event.payload['reason'] == '运营处理'
