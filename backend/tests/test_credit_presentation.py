"""Account presentation must distinguish executable collateral from MTM."""
from decimal import Decimal
from datetime import datetime, timezone
import pytest
from app.core.database import async_session_maker
from app.models.fx import FxPair, FxWallet
from app.services.credit import flags
from tests.test_user_summary_contract import _make_user, _seed_position

@pytest.fixture(autouse=True)
def unified_flags():
    flags.set_flags(flags.CreditFlags(
        credit_leverage=Decimal('20'), credit_maintenance_ratio=Decimal('.04')))
    yield
    flags.clear_flags()
    flags.set_new_risk_frozen(None)

@pytest.mark.asyncio
async def test_summary_debt_free_still_uses_actual_lcv_and_multiple_wallets(client):
    uid, headers = await _make_user()
    await _seed_position(uid, shares='100', total_shares='100')
    async with async_session_maker() as db:
        for code in ['MORA', 'LMD']:
            pair = FxPair(currency_code=code, currency_name=code, status='trading',
                          gold_reserve=Decimal('1000'), foreign_reserve=Decimal('5000'))
            db.add(pair)
            await db.flush()
            db.add(FxWallet(user_id=uid, pair_id=pair.id, foreign_amount=Decimal('100'), cost_basis=Decimal('15')))
        await db.commit()
    response = await client.get('/api/v1/user/summary', headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()

    assert data['display_equity'] > data['liquidation_equity']
    assert data['debt_with_interest'] == 0
    assert data['risk_status'] == 'healthy'
    assert {w['currency_code'] for w in data['fx_wallets']} == {'MORA', 'LMD'}
    assert all(w['mtm_gold'] == 20 for w in data['fx_wallets'])
    assert all(w['cost_basis'] == 15 and w['currency_name'] == w['currency_code'] for w in data['fx_wallets'])
    assert data['fx_cost_basis'] == 30
    assert data['fx_unrealized_pnl'] == 10

@pytest.mark.asyncio
async def test_policy_reports_running_thresholds_and_marks_old_fields(client):
    from app.models.base import SiteConfig
    async with async_session_maker() as db:
        for key, value, kind in [
            ('liquidation_enabled', 'true', 'bool'),
            ('liquidation_hard_threshold', '.2', 'decimal'),
            ('liquidation_soft_threshold', '.5', 'decimal'),
            ('liquidation_partial_pct', '.25', 'decimal'),
            ('liquidation_target_margin', '.8', 'decimal'),
            ('liquidation_emergency_threshold', '0', 'decimal'),
            ('liquidation_sweep_interval_sec', '600', 'int'),
        ]:
            db.add(SiteConfig(key=key, value=value, value_type=kind))
        await db.commit()
    response = await client.get('/api/v1/loan/liquidation-policy')
    assert response.status_code == 200, response.text
    data = response.json()

    assert data['r_initial'] == pytest.approx(1 / 19)
    assert data['r_maintenance'] == .04
    assert data['partial_pct'] == .25

    assert 'sell_fee_rate' in data and 'fx_sell_fee_rates' in data

@pytest.mark.asyncio
async def test_summary_paused_fx_has_display_value_but_no_collateral(client):
    uid, headers = await _make_user(cash=Decimal('100'), debt=Decimal('100'))
    async with async_session_maker() as db:
        pair = FxPair(currency_code='MORA', currency_name='Mora', status='paused',
                      gold_reserve=Decimal('1000'), foreign_reserve=Decimal('5000'))
        db.add(pair)
        await db.flush()
        db.add(FxWallet(user_id=uid, pair_id=pair.id, foreign_amount=Decimal('1000')))
        await db.commit()
    data = (await client.get('/api/v1/user/summary', headers=headers)).json()
    assert data['display_equity'] == 200
    assert data['liquidation_equity'] == 0
    assert data['risk_status'] == 'danger'

@pytest.mark.asyncio
async def test_public_liquidation_exposes_product_only(client):
    from app.models.base import LiquidationEvent
    uid, _ = await _make_user()
    async with async_session_maker() as db:
        db.add(LiquidationEvent(user_id=uid, triggered_at=datetime.now(timezone.utc), product='fx', pre_cash=0, pre_debt=100,
            pre_holdings_value=90, pre_net_worth=-10, pre_margin_ratio=Decimal('-.1'),
            sold_positions_count=1, total_proceeds=90, repaid_amount=90,
            remaining_debt=10, post_cash=0, trigger_source='scheduler', mode='partial'))
        await db.commit()
    data = (await client.get('/api/v1/loan/recent-liquidations')).json()
    assert data[0]['product'] == 'fx'
    assert 'run_id' not in data[0] and 'actions' not in data[0]

@pytest.mark.asyncio
@pytest.mark.parametrize('status,quantity,expected', [
    ('trading', '100', 'danger'), ('trading', '5000', 'blocked'), ('paused', '100', 'danger')])
async def test_short_account_and_quota_keep_gold_debt_separate(client, status, quantity, expected):
    """Locked proceeds are not spendable; unquotable debt cannot appear healthy."""
    from app.models.base import SiteConfig, LiquidationEvent
    from app.models.fx import FxShortPosition
    uid, headers = await _make_user(cash=Decimal('21'))
    clock = datetime.now(timezone.utc)
    async with async_session_maker() as db:
        if expected == 'blocked':
            from app.models.base import User
            from sqlalchemy import select
            own = (await db.execute(select(User).where(User.id == uid))).scalar_one()
            own.credit_frozen = True
        db.add_all([SiteConfig(key=k, value=v, value_type=t) for k,v,t in [
            ('loan_daily_rate', '.001', 'decimal'), ('loan_enabled', 'true', 'bool'),
            ('fx_enabled', 'true', 'bool')]])
        pair = FxPair(currency_code='MORA', currency_name='Mora', status=status,
                      gold_reserve=Decimal('1000'), foreign_reserve=Decimal('5000'))
        db.add(pair)
        await db.flush()
        pid = pair.id
        db.add(FxShortPosition(user_id=uid, pair_id=pid,
            principal_foreign=Decimal(quantity), interest_foreign=Decimal('1'),
            restricted_gold=Decimal('20'), proceeds_basis_gold=Decimal('20'),
            interest_last_accrued_at=clock))
        db.add(LiquidationEvent(user_id=uid, triggered_at=clock, product='fx',
            pre_cash=21, pre_debt=0, pre_holdings_value=None, pre_net_worth=None,
            pre_margin_ratio=None, sold_positions_count=0, total_proceeds=0, repaid_amount=0,
            remaining_debt=0, post_cash=21, trigger_source='scheduler', mode='partial'))
        await db.commit()
    response = await client.get('/api/v1/user/summary', headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['cash'] == 21 and Decimal(str(data['available_cash'])) == 1
    assert Decimal(str(data['restricted_cash'])) == 20
    assert data['debt'] == data['debt_with_interest'] == 0
    assert data['risk_status'] == expected
    short = data['short_positions'][0]
    assert short['pair_id'] == pid and Decimal(short['pending_short_debt']) > Decimal(quantity)
    assert (short['reference_cover_cost'] is None if expected == 'blocked' else
            Decimal(short['reference_cover_cost']) == Decimal(str(data['short_cover_cost'])))
    assert 'short_lending_limit_foreign' not in short and 'treasury' not in short
    response = await client.get('/api/v1/loan/quota', headers=headers)
    assert response.status_code == 200, response.text
    quota = response.json()
    assert Decimal(quota['debt']) == 0 and Decimal(quota['max_borrow']) == 0
    assert quota['risk_status'] == expected
    if expected == 'blocked':
        assert data['short_cover_cost'] is data['risk_basis'] is data['equity_to_risk_basis'] is None
        assert data['liquidation_equity'] is None and data['blocked_reason']
        assert quota['net_worth'] is None and quota['blocked_reason']
        assert quota['borrow_blocked_reason'] == data['borrow_blocked_reason'] == 'credit_frozen'
    else:
        assert Decimal(str(data['short_cover_cost'])) > 20
        assert Decimal(str(data['risk_basis'])) > 0 and data['equity_to_risk_basis'] is not None
        assert short['executable'] is (status == 'trading')
        if status == 'paused':
            assert short['blocked_reason'] and short['reference_cover_cost'] is not None
    public = await client.get('/api/v1/loan/recent-liquidations')
    assert public.status_code == 200 and public.json()[0]['pre_net_worth'] is None
    assert public.json()[0]['pre_holdings_value'] is None
    async with async_session_maker() as db:
        from sqlalchemy import select
        row = (await db.execute(select(FxShortPosition).where(
            FxShortPosition.user_id == uid, FxShortPosition.pair_id == pid))).scalar_one()
        assert row.interest_last_accrued_at.replace(tzinfo=timezone.utc) == clock
        # Module-scoped app startup rejects leftover live debt with legacy flags.
        await db.delete(row)
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled,operator,user_frozen,reason', [
    (False, True, True, 'loan_disabled'),
    (True, True, True, 'frozen_by_operator'),
    (True, False, True, 'credit_frozen'),
])
async def test_healthy_frozen_account_explains_borrow_gate_and_can_repay(
        client, enabled, operator, user_frozen, reason):
    from app.models.base import SiteConfig, User
    from sqlalchemy import select
    uid, headers = await _make_user(cash=Decimal('100'), debt=Decimal('1'))
    async with async_session_maker() as db:
        row = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        row.credit_frozen = user_frozen
        db.add_all([SiteConfig(key=k, value=v, value_type=t) for k,v,t in [
            ('loan_enabled', str(enabled).lower(), 'bool'),
            ('credit_new_risk_frozen', str(operator).lower(), 'bool'),
            ('loan_daily_rate', '0', 'decimal')]])
        await db.commit()
    for endpoint in ['user/summary', 'loan/quota']:
        response = await client.get('/api/v1/' + endpoint, headers=headers)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['risk_status'] == 'healthy'
        assert data['blocked_reason'] is None
        assert data['credit_frozen'] is True and data['new_risk_frozen'] is operator
        assert data['borrow_blocked_reason'] == reason
        if endpoint == 'loan/quota':
            assert Decimal(data['max_borrow']) == 0
    response = await client.post('/api/v1/loan/repay-all', headers=headers)
    assert response.status_code == 200, response.text
    assert Decimal(response.json()['debt']) == 0
