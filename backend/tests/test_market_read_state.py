"""Committed snapshot invariants and the public/covered read boundary."""
import asyncio
from dataclasses import FrozenInstanceError, asdict, replace
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, update

from app.core.database import async_session_maker
from app.models.base import Market, Outcome
from app.models.fx import FxPair
from app.schemas.trading_context import GroupKey
from app.services.credit import flags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey as GateKey
from app.services.credit.ownership import OWNERSHIP
from app.services.market_read_state import FxReadState
from app.services.trading_prices import PricingRegistry


async def create_pair():
    async with async_session_maker() as db:
        pair = FxPair(currency_code='SNAP', currency_name='Snapshot',
                      gold_reserve=Decimal('123.456789'),
                      buy_fee_rate=Decimal('0.12345678'))
        db.add(pair)
        await db.commit()
        await db.refresh(pair)
        return FxReadState.from_pair(pair)


def memory_registry():
    calls = []

    def factory():
        calls.append(True)
        return async_session_maker()

    registry = PricingRegistry(factory)
    registry.configure(memory_products=frozenset({'fx', 'lmsr'}))
    return registry, calls


@pytest.mark.asyncio
async def test_warm_snapshots_preserve_stamps_precision_and_observation_time():
    state = await create_pair()
    registry, calls = memory_registry()
    await registry.warm_fx()
    key = GroupKey(product='fx', id=state.id)
    first = (await registry.read_groups([key]))[0]
    second = (await registry.read_groups([key]))[0]
    assert len(calls) == 1
    assert first.stamp == second.stamp
    assert first.as_of == second.as_of
    assert first.gold_reserve == '123.456789'
    assert first.buy_fee_rate == '0.12345678'

    registry.install_committed(replace(state, currency_name='New name'))
    renamed = (await registry.read_groups([key]))[0]
    assert renamed.stamp == first.stamp
    assert registry.peek_fx(state.id).currency_name == 'New name'
    registry.install_committed(replace(state, gold_reserve=Decimal('124.456789')))
    changed = (await registry.read_groups([key]))[0]
    assert int(changed.stamp.revision) == int(first.stamp.revision) + 1
    assert changed.stamp.input_hash != first.stamp.input_hash
    assert len(calls) == 1
    first.stamp.revision = '999'
    assert (await registry.read_groups([key]))[0].stamp == changed.stamp
    with pytest.raises(FrozenInstanceError):
        registry.peek_fx(state.id).currency_name = 'corrupted'


@pytest.mark.asyncio
async def test_lmsr_decimal_inputs_and_returned_lists_are_isolated():
    async with async_session_maker() as db:
        market = Market(title='Snapshot', liquidity_b=1.234567890123)
        db.add(market)
        await db.flush()
        db.add_all([Outcome(market_id=market.id, label='A', total_shares=Decimal('3.123456')),
                    Outcome(market_id=market.id, label='B', total_shares=Decimal('7.654321'))])
        await db.commit()
    registry, calls = memory_registry()
    key = GroupKey(product='lmsr', id=market.id)
    first = (await registry.read_groups([key]))[0]
    assert first.b == '1.234567890123'
    assert first.q_dec == ['3.123456', '7.654321']
    first.q_dec[0] = '999'
    first.outcome_ids.clear()
    first.stamp.input_hash = 'corrupted'
    second = (await registry.read_groups([key]))[0]
    assert second.q_dec == ['3.123456', '7.654321']
    assert len(second.outcome_ids) == 2
    assert second.stamp.input_hash != 'corrupted'
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_public_warm_read_passes_writer_and_covered_read_waits_for_install():
    state = await create_pair()
    registry, calls = memory_registry()
    registry.install_committed(state)
    key = GroupKey(product='fx', id=state.id)
    held, release = asyncio.Event(), asyncio.Event()

    async def writer():
        async with GATES.hold(exclusive=[GateKey('fx', state.id)]):
            held.set()
            await release.wait()
            registry.install_committed(replace(state, pool_version=2))

    writing = asyncio.create_task(writer())
    await held.wait()
    covered = asyncio.create_task(registry.read_groups([key]))
    try:
        public = await asyncio.wait_for(registry.read_groups([key], require_coverage=False), 0.5)
        direct = await asyncio.wait_for(registry.read_fx(state.id), 0.5)
        assert public[0].pool_version == '1' and direct.pool_version == 1
        assert not covered.done()
    finally:
        release.set()
        await writing
    assert (await covered)[0].pool_version == '2'
    assert calls == []
    async with GATES.hold(shared=[GateKey('fx', state.id)]):
        assert (await registry.read_groups_locked([key]))[0].pool_version == '2'


@pytest.mark.asyncio
async def test_database_mode_missing_and_recreated_identity_advance_revision():
    state = await create_pair()
    registry = PricingRegistry(async_session_maker)
    key = GroupKey(product='fx', id=state.id)
    first = (await registry.read_groups([key]))[0]
    async with async_session_maker() as db:
        await db.execute(delete(FxPair).where(FxPair.id == state.id))
        await db.commit()
    assert await registry.read_groups([key]) == []
    assert await registry.read_groups([key]) == []
    with pytest.raises(HTTPException) as exc:
        await registry.read_fx(state.id)
    assert exc.value.status_code == 404
    async with async_session_maker() as db:
        db.add(FxPair(**asdict(state)))
        await db.commit()
    restored = (await registry.read_groups([key]))[0]
    assert restored.stamp.input_hash == first.stamp.input_hash
    assert int(restored.stamp.revision) == int(first.stamp.revision) + 2


@pytest.mark.asyncio
async def test_invalidation_reload_and_owner_or_readonly_database_fallback(monkeypatch):
    state = await create_pair()
    registry, calls = memory_registry()
    key = GroupKey(product='fx', id=state.id)
    registry.install_committed(state)
    registry.invalidate(key)
    reloaded = await registry.read_fx(state.id)
    assert reloaded.gold_reserve == Decimal('123.456789')
    assert len(calls) == 1
    async with async_session_maker() as db:
        await db.execute(update(FxPair).where(FxPair.id == state.id).values(pool_version=7))
        await db.commit()
    monkeypatch.setattr(OWNERSHIP, '_writes_enabled', False)
    assert registry.peek_fx(state.id) is None
    assert (await registry.read_fx(state.id)).pool_version == 7
    monkeypatch.setattr(OWNERSHIP, '_writes_enabled', True)
    monkeypatch.setenv(flags.READ_ONLY_ENV, 'true')
    assert registry.peek_fx(state.id) is None
    assert (await registry.read_fx(state.id)).pool_version == 7
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_late_missing_cold_load_keeps_newly_committed_creation():
    state = await create_pair()
    async with async_session_maker() as db:
        await db.execute(delete(FxPair).where(FxPair.id == state.id))
        await db.commit()
    registry, calls = memory_registry()
    queried, proceed = asyncio.Event(), asyncio.Event()
    snapshot = registry._snapshot

    async def delayed_snapshot(db, key):
        result = await snapshot(db, key)
        assert result is None
        queried.set()
        await proceed.wait()
        return result

    registry._snapshot = delayed_snapshot
    reading = asyncio.create_task(registry.read_fx(state.id))
    await queried.wait()
    # Creation is the explicit no-GATE exception; its snapshot wins over None.
    async with async_session_maker() as db:
        db.add(FxPair(id=state.id, currency_code='CREATED', currency_name='Created'))
        await db.commit()
        pair = await db.get(FxPair, state.id)
        await db.refresh(pair)
        committed = FxReadState.from_pair(pair)
    registry.install_committed(committed)
    proceed.set()
    assert (await reading).currency_code == 'CREATED'
    assert registry.peek_fx(state.id) == committed
    assert (await registry.read_fx(state.id)).currency_code == 'CREATED'
    assert len(calls) == 1
