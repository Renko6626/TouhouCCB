"""Administrator projections preserve balances and cross-product history."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.audit import AuditEvent
from app.models.base import Market, Outcome, Position, SiteConfig, Transaction, User
from app.models.fx import FxPair, FxShortPosition, FxTrade, FxWallet
from app.services import admin_user_reads as reads, site_config
from app.services.admin_user_service import AdminUserError


async def seed(db):
    now = datetime.now(timezone.utc)
    user = User(username='disabled', cash=Decimal('123.5'), debt=Decimal('4'), is_active=False,
                email='private@example.com', casdoor_id='private-id')
    bot = User(username='999999999999999999999999', is_bot=True)
    empty = User(username='empty')
    pair = FxPair(currency_code='TH', currency_name='幻想币')
    market = Market(title='预测市场')
    db.add_all([user, bot, empty, pair, market])
    await db.flush()
    outcome = Outcome(market_id=market.id, label='会发生')
    db.add(outcome)
    await db.flush()
    db.add_all([
        FxWallet(user_id=user.id, pair_id=pair.id, foreign_amount=Decimal('3'), cost_basis=Decimal('9')),
        FxShortPosition(user_id=user.id, pair_id=pair.id, principal_foreign=Decimal('10'),
                        interest_foreign=Decimal('1'), proceeds_basis_gold=Decimal('12'),
                        interest_last_accrued_at=now - timedelta(days=1)),
        Position(user_id=user.id, outcome_id=outcome.id, amount=Decimal('2'), cost_basis=Decimal('0.8')),
        AuditEvent(user_id=user.id, event_type='user_register', ts=now - timedelta(days=3)),
        AuditEvent(user_id=user.id, event_type='admin_ban', ts=now,
                   operator_user_id=empty.id, payload={'reason': '违规', 'ip': 'private'}),
        AuditEvent(user_id=user.id, event_type='liquidation_action', ts=now + timedelta(seconds=1),
                   operator_user_id=empty.id, payload={'reason': '管理员平仓'}),
        AuditEvent(user_id=user.id, event_type='liquidation_action', ts=now + timedelta(seconds=2),
                   payload={'reason': '系统强平'}),
        Transaction(user_id=user.id, outcome_id=outcome.id, type='settle_lose',
                    shares=Decimal('1'), cost=Decimal('0'), timestamp=now - timedelta(minutes=2)),
        Transaction(user_id=user.id, outcome_id=outcome.id, type='sell',
                    shares=Decimal('-2'), cost=Decimal('-1.5'), timestamp=now),
        FxTrade(user_id=user.id, pair_id=pair.id, purpose='short_open', side='sell',
                input_amount=Decimal('2'), output_amount=Decimal('4'),
                pre_gold_reserve=Decimal('100'), pre_foreign_reserve=Decimal('100'),
                post_gold_reserve=Decimal('96'), post_foreign_reserve=Decimal('102'),
                post_price=Decimal('0.94'), created_at=now - timedelta(minutes=1)),
    ])
    await db.commit()
    return user.id, bot.id, empty.id, now


@pytest.mark.asyncio
async def test_list_filters_and_real_enrichment():
    async with async_session_maker() as db:
        uid, bot_id, empty_id, now = await seed(db)
        rows = await reads.list_users(db, user_ids=[uid, empty_id], has_positions=True, is_active=False)
        assert len(rows) == 1
        row = rows[0]
        assert (row['id'], row['cash'], row['debt']) == (uid, 123.5, 4.0)
        assert (row['spot_count'], row['short_count'], row['lmsr_count']) == (1, 1, 1)
        assert row['registered_at'] == now - timedelta(days=3)
        assert row['last_trade_at'] == now
        assert {'email', 'casdoor_id', 'ip'}.isdisjoint(row)
        assert [r['id'] for r in await reads.list_users(db, has_positions=False, include_bots=False)] == [empty_id]
        assert [r['id'] for r in await reads.list_users(db, has_shorts=True)] == [uid]
        assert [r['id'] for r in await reads.list_users(db, user_ids=[bot_id, empty_id], offset=1, limit=1)] == [empty_id]
        assert [r['id'] for r in await reads.list_users(db, q=str(uid))] == [uid]
        assert [r['id'] for r in await reads.list_users(db, q='999999999999999999999999')] == [bot_id]
        assert await reads.list_users(db, user_ids=[]) == []


@pytest.mark.asyncio
async def test_disabled_activity_accrues_read_only_and_combines_bounded_trades():
    async with async_session_maker() as db:
        uid, _, operator, now = await seed(db)
        db.add(SiteConfig(key='loan_daily_rate', value='0.1', value_type='decimal'))
        site_config.clear_cache()
        await db.commit()
        before = (await db.execute(select(FxShortPosition).where(FxShortPosition.user_id == uid))).scalar_one()
        original = (before.interest_foreign, before.interest_last_accrued_at)
        result = await reads.get_activity(db, uid, limit=2)
        assert [(p['product'], p['kind'], p['market_name'], p['asset_name']) for p in result['positions']] == [
            ('fx', 'spot', '幻想币', '幻想币'), ('fx', 'short', '幻想币', '幻想币'), ('lmsr', 'spot', '预测市场', '会发生')]
        short = result['positions'][1]
        assert short['quantity'] == 10.0
        assert short['interest'] == pytest.approx(2.1, abs=0.00001)
        assert short['cost_basis'] == 12.0
        assert [(t['product'], t['side'], t['gold']) for t in result['transactions']] == [
            ('lmsr', 'sell', -1.5), ('fx', 'sell', -4.0)]
        assert result['transactions'][1]['purpose'] == 'short_open'
        assert result['last_trade_at'] == now
        assert [(h['created_at'], h['event_type'], h['operator_user_id'], h['reason'])
                for h in result['history']] == [
            (now + timedelta(seconds=1), 'liquidation_action', operator, '管理员平仓'),
            (now, 'admin_ban', operator, '违规'),
        ]
        assert (await reads.get_activity(db, uid, limit=3))['transactions'][-1]['side'] == 'settle_lose'
        await db.commit()
        await db.refresh(before)
        user = await db.get(User, uid)
        assert (before.interest_foreign, before.interest_last_accrued_at) == original
        assert (user.cash, user.debt, user.is_active) == (Decimal('123.5'), Decimal('4'), False)
        assert len((await db.execute(select(AuditEvent).where(AuditEvent.user_id == uid))).scalars().all()) == 4


@pytest.mark.asyncio
async def test_activity_unknown_user_is_not_found():
    async with async_session_maker() as db:
        with pytest.raises(AdminUserError) as error:
            await reads.get_activity(db, 999999)
        assert error.value.status == 404
