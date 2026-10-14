from decimal import Decimal
from datetime import datetime, timezone
import pytest
from sqlalchemy import delete, update
from app.core.database import async_session_maker
from app.models.base import User, Market, Outcome, Position
from app.models.fx import FxPair, FxWallet, FxShortPosition
from app.schemas.trading_context import GroupKey
from app.services.trading_prices import PricingRegistry
from app.services.credit.trading_context import read_trading_context, read_trading_versions, read_trading_prices
from app.services.credit import flags

@pytest.mark.asyncio
async def test_complete_private_basis_and_missing_identity():
    async with async_session_maker() as db:
        clock = datetime.now(timezone.utc)
        user, other = User(username='basis', cash=Decimal('100'), debt=Decimal('20'), debt_last_accrued_at=clock), User(username='other')
        market = Market(title='q', liquidity_b=1.234567890123)
        pairs = [FxPair(currency_code=str(i), currency_name=str(i), buy_fee_rate=Decimal('0.12345678')) for i in (1,2)]
        db.add_all([user, other, market, *pairs]); await db.flush()
        outcomes = [Outcome(market_id=market.id, label=str(i), total_shares=Decimal(i)) for i in (3,7)]
        db.add_all(outcomes); await db.flush()
        db.add_all([Position(user_id=user.id,outcome_id=outcomes[0].id,amount=Decimal('2'),cost_basis=Decimal('1')), Position(user_id=other.id,outcome_id=outcomes[1].id,amount=Decimal('99')), FxWallet(user_id=user.id,pair_id=pairs[0].id,foreign_amount=Decimal('10')), FxShortPosition(user_id=user.id,pair_id=pairs[1].id,principal_foreign=Decimal('5'),interest_foreign=Decimal('1'),interest_last_accrued_at=clock,proceeds_basis_gold=Decimal('6'))]); await db.commit()
        registry = PricingRegistry(async_session_maker)
        result = await read_trading_context(db,user.id,registry=registry)
        assert result.account.cash == '100.000000' and result.account.debt == '20.000000'
        assert result.account.debt_last_accrued_at and len(result.account.lmsr_positions)==1

        assert result.account.fx_shorts[0].proceeds_basis_gold == '6.000000'
        assert len(result.groups)==3
        lmsr = next(g for g in result.groups if g.product=='lmsr')
        assert lmsr.q_dec == ['3.000000','7.000000'] and lmsr.b=='1.234567890123'
        assert next(g for g in result.groups if g.product=='fx').buy_fee_rate=='0.12345678'
        versions=await read_trading_versions(db,user.id,registry=registry)
        assert versions.group_stamps==result.minimum_group_stamps
        with pytest.raises(Exception) as exc:
            await read_trading_prices(db,user.id,[GroupKey(product='fx',id=999)],registry=registry)
        assert exc.value.status_code==403
        await db.execute(delete(Outcome).where(Outcome.id==outcomes[0].id))
        await db.execute(delete(FxPair).where(FxPair.id==pairs[0].id)); await db.commit()
        broken=await read_trading_context(db,user.id,registry=registry)
        assert broken.account.lmsr_positions[0].market_id is None
        assert GroupKey(product='fx',id=pairs[0].id) in broken.missing_groups

@pytest.mark.asyncio
async def test_effective_rules_and_complete_revision():
    async with async_session_maker() as db:
        user=User(username='rules'); pair=FxPair(currency_code='R',currency_name='R')
        db.add_all([user,pair]); await db.commit()
        flags.set_flags(flags.CreditFlags(credit_leverage=Decimal('5'),credit_maintenance_ratio=Decimal('0.03')))
        try:
            registry=PricingRegistry(async_session_maker); key=GroupKey(product='fx',id=pair.id)
            first=await read_trading_context(db,user.id,key,registry=registry)
            assert first.rules.leverage=='5' and first.rules.initial_ratio=='0.25'
            await db.execute(update(FxPair).where(FxPair.id==pair.id).values(sell_fee_rate=Decimal('0.12345678')));await db.commit()
            second=await registry.refresh_groups([key]); third=await registry.refresh_groups([key])
            assert int(second[0].stamp.revision)>int(first.groups[0].stamp.revision)
            assert third[0].stamp==second[0].stamp
        finally: flags.clear_flags()
