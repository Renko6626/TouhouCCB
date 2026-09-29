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
    flags.set_flags(flags.CreditFlags(unified_credit_enabled=True,
        credit_leverage=Decimal('20'), credit_maintenance_ratio=Decimal('.04')))
    yield
    flags.clear_flags()

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
            db.add(FxWallet(user_id=uid, pair_id=pair.id, foreign_amount=Decimal('100')))
        await db.commit()
    response = await client.get('/api/v1/user/summary', headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['unified_credit_enabled'] is True
    assert data['display_equity'] > data['liquidation_equity']
    assert data['equity_to_debt'] is None
    assert data['debt_with_interest'] == 0
    assert data['risk_status'] == 'healthy'
    assert {w['currency_code'] for w in data['fx_wallets']} == {'MORA', 'LMD'}
    assert all(w['mtm_gold'] == 20 for w in data['fx_wallets'])

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
    assert data['unified_credit_enabled'] is True
    assert data['r_initial'] == pytest.approx(1 / 19)
    assert data['r_maintenance'] == .04
    assert data['partial_pct'] == .25
    assert data['legacy']['legacy'] is True
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
    assert data['equity_to_debt'] == 0
    assert data['risk_status'] == data['margin_status'] == 'danger'

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
