from decimal import Decimal
from datetime import datetime, timezone
import pytest
from sqlalchemy import delete, update
from app.core.database import async_session_maker
from app.models.base import User, Market, Outcome, Position
from app.models.fx import FxPair, FxWallet, FxShortPosition
from app.schemas.trading_context import GroupKey
from app.services.trading_prices import PricingRegistry
from app.services.credit.trading_context import read_trading_context, read_trading_versions, read_trading_prices, read_dependencies
from app.services.credit import flags

@pytest.mark.asyncio
async def test_complete_private_basis_and_missing_identity():
    async with async_session_maker() as db:
        clock = datetime.now(timezone.utc)
        user, other = User(username='basis', cash=Decimal('100'), debt=Decimal('20'), debt_last_accrued_at=clock), User(username='other')
        market = Market(title='q', liquidity_b=1.234567890123)
        pairs = [FxPair(currency_code=str(i), currency_name=str(i), buy_fee_rate=Decimal('0.12345678')) for i in (1,2,3,4)]
        db.add_all([user, other, market, *pairs]); await db.flush()
        outcomes = [Outcome(market_id=market.id, label=str(i), total_shares=Decimal(i)) for i in (3,7)]
        db.add_all(outcomes); await db.flush()
        db.add_all([Position(user_id=user.id,outcome_id=outcomes[0].id,amount=Decimal('2'),cost_basis=Decimal('1')), Position(user_id=other.id,outcome_id=outcomes[1].id,amount=Decimal('99')), FxWallet(user_id=user.id,pair_id=pairs[0].id,foreign_amount=Decimal('10')), FxShortPosition(user_id=user.id,pair_id=pairs[1].id,principal_foreign=Decimal('5'),interest_foreign=Decimal('1'),interest_last_accrued_at=clock,proceeds_basis_gold=Decimal('6'))]); await db.commit()
        db.add_all([
            Position(user_id=user.id, outcome_id=outcomes[1].id, amount=Decimal('0')),
            FxWallet(user_id=user.id, pair_id=pairs[1].id, foreign_amount=Decimal('0')),
            FxWallet(user_id=user.id, pair_id=pairs[2].id, foreign_amount=Decimal('0')),
            FxShortPosition(user_id=user.id, pair_id=pairs[3].id, principal_foreign=Decimal('0')),
        ]); await db.commit()
        registry = PricingRegistry(async_session_maker)
        current = GroupKey(product='lmsr', id=999)
        result = await read_trading_context(db,user.id,current,registry=registry)
        assert result.account.cash == '100.000000' and result.account.debt == '20.000000'
        assert result.account.debt_last_accrued_at and len(result.account.lmsr_positions)==2

        assert result.account.fx_shorts[0].proceeds_basis_gold == '6.000000'
        assert [(g.product, g.id) for g in result.groups] == [('fx', p.id) for p in pairs] + [('lmsr', market.id)]
        assert result.missing_groups == [current]
        expected_dependencies = [GroupKey(product='fx', id=p.id) for p in pairs] + [GroupKey(product='lmsr', id=market.id)]
        assert await read_dependencies(db, user.id, current) == expected_dependencies + [current]
        lmsr = next(g for g in result.groups if g.product=='lmsr')
        assert lmsr.q_dec == ['3.000000','7.000000'] and lmsr.b=='1.234567890123'
        assert next(g for g in result.groups if g.product=='fx').buy_fee_rate=='0.12345678'
        versions=await read_trading_versions(db,user.id,current,registry=registry)
        assert versions.group_stamps==result.minimum_group_stamps
        with pytest.raises(Exception) as exc:
            await read_trading_prices(db,user.id,[GroupKey(product='fx',id=999)],registry=registry)
        assert exc.value.status_code==403
        await db.execute(delete(Outcome).where(Outcome.id==outcomes[0].id))
        await db.execute(delete(FxPair).where(FxPair.id==pairs[0].id)); await db.commit()
        broken=await read_trading_context(db,user.id,registry=registry)
        assert broken.account.lmsr_positions[0].market_id is None
        assert GroupKey(product='fx',id=pairs[0].id) in broken.missing_groups
        assert await read_dependencies(db, user.id) == expected_dependencies

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


@pytest.mark.asyncio
async def test_lmsr_read_only_process_keeps_authoritative_database_reads(monkeypatch):
    async with async_session_maker() as db:
        market = Market(title='readonly', liquidity_b=1.234567890123)
        db.add(market)
        await db.flush()
        outcome = Outcome(market_id=market.id, label='A', total_shares=Decimal('1.123456'))
        db.add(outcome)
        await db.commit()
        registry = PricingRegistry(async_session_maker)
        registry.configure(memory_products=frozenset({'lmsr'}))
        key = GroupKey(product='lmsr', id=market.id)
        assert (await registry.read_groups([key]))[0].q_dec == ['1.123456']
        monkeypatch.setenv(flags.READ_ONLY_ENV, 'true')
        await db.execute(update(Outcome).where(Outcome.id == outcome.id).values(total_shares=Decimal('2.654321')))
        await db.commit()
        assert (await registry.read_groups([key]))[0].q_dec == ['2.654321']
        await db.execute(update(Outcome).where(Outcome.id == outcome.id).values(total_shares=Decimal('3.987654')))
        await db.commit()
        assert (await registry.read_groups([key]))[0].q_dec == ['3.987654']


@pytest.mark.asyncio
async def test_pricing_publisher_shares_serialization_and_filters_dependencies(monkeypatch):
    import asyncio
    from dataclasses import replace
    from sqlalchemy import event
    from app.core.database import engine
    from app.schemas.trading_context import FxPricingGroup
    from app.services import trading_prices
    from app.services.account_events import Subscription
    from app.services.market_read_state import FxReadState

    async with async_session_maker() as db:
        pairs = [FxPair(currency_code=str(i), currency_name=str(i)) for i in (1, 2)]
        db.add_all(pairs)
        await db.commit()
        for pair in pairs:
            await db.refresh(pair)
        states = [FxReadState.from_pair(pair) for pair in pairs]
    registry = PricingRegistry(async_session_maker)
    registry.configure(memory_products=frozenset({'fx'}))
    for state in states:
        registry.install_committed(state)
    monkeypatch.setattr(trading_prices, 'REGISTRY', registry)
    keys = [GroupKey(product='fx', id=state.id) for state in states]
    before = await registry.read_groups(keys, require_coverage=False)
    again = await registry.read_groups(keys, require_coverage=False)
    assert [(g.stamp, g.as_of) for g in before] == [(g.stamp, g.as_of) for g in again]
    dumps, statements = [], []
    original = FxPricingGroup.model_dump
    def dumped(group, *args, **kwargs):
        dumps.append(group.id)
        return original(group, *args, **kwargs)
    def observed(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    monkeypatch.setattr(FxPricingGroup, 'model_dump', dumped)
    event.listen(engine.sync_engine, 'before_cursor_execute', observed)
    publisher = trading_prices.PricingPublisher()
    all_groups, first_only, second_only = [Subscription(i) for i in (11, 22, 33)]
    publisher.subscribe(all_groups, keys)
    publisher.subscribe(first_only, keys[:1])
    publisher.subscribe(second_only, keys[1:])
    publisher.start()
    publisher.wake.set()
    try:
        _, both = await asyncio.wait_for(all_groups.queue.get(), 2)
        _, first = await asyncio.wait_for(first_only.queue.get(), 2)
        _, second = await asyncio.wait_for(second_only.queue.get(), 2)
        assert [g['id'] for g in first['groups']] == [states[0].id]
        assert [g['id'] for g in second['groups']] == [states[1].id]
        assert both['groups'][0] is first['groups'][0]
        assert both['groups'][1] is second['groups'][0]
        assert sorted(dumps) == sorted(state.id for state in states)
        registry.install_committed(replace(states[0], gold_reserve=Decimal('2'), pool_version=2))
        publisher.wake.set()
        _, changed = await asyncio.wait_for(all_groups.queue.get(), 2)
        _, changed_first = await asyncio.wait_for(first_only.queue.get(), 2)
        assert changed['groups'][0]['gold_reserve'] == '2'
        assert changed['groups'][0]['stamp'] != first['groups'][0]['stamp']
        assert changed['groups'][0] is changed_first['groups'][0]
        assert second_only.queue.empty()
        assert dumps.count(states[0].id) == 2 and dumps.count(states[1].id) == 1
        assert statements == []
    finally:
        await publisher.stop()
        event.remove(engine.sync_engine, 'before_cursor_execute', observed)
