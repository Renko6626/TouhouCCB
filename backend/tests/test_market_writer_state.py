"""MarketWriter 状态加载/注册/重置单元测试（不走 HTTP，不需要 client fixture）。"""
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import update

import pytest
from fastapi import HTTPException
import pytest_asyncio
from sqlmodel import SQLModel

from app.core.database import engine, async_session_maker
from app.models.base import Market, MarketStatus, Outcome
from app.schemas.trading_context import GroupKey
from app.services import trading_prices
from app.services.market_writer import WRITER


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db(monkeypatch):
    registry = trading_prices.PricingRegistry(async_session_maker)
    registry.configure(memory_products=frozenset({'lmsr'}))
    monkeypatch.setattr(trading_prices, 'REGISTRY', registry)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    yield
    await WRITER.stop()


async def _seed_market(status=MarketStatus.TRADING, shares=("3.5", "0"), b=100.0) -> tuple[int, list[int]]:
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=b, status=status)
        s.add(m)
        await s.flush()
        oids = []
        for v in shares:
            o = Outcome(market_id=m.id, label=f"o{v}", total_shares=Decimal(v))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        await s.commit()
        return m.id, oids


@pytest.mark.asyncio
async def test_start_loads_trading_and_halt_not_settled():
    mid_t, _ = await _seed_market(MarketStatus.TRADING)
    mid_h, _ = await _seed_market(MarketStatus.HALT)
    mid_s, _ = await _seed_market(MarketStatus.SETTLED)
    await WRITER.start()
    assert WRITER.enabled
    assert WRITER.get_state(mid_t) is not None
    assert WRITER.get_state(mid_h) is not None
    assert WRITER.get_state(mid_s) is None  # spec § 4.1: SETTLED 不载入
    # Writer absence is not a missing market: terminal markets cold-load.
    settled = (await trading_prices.REGISTRY.read_groups([GroupKey(product='lmsr', id=mid_s)]))[0]
    assert settled.status == MarketStatus.SETTLED
    assert settled.q_dec == ['3.500000', '0.000000']


@pytest.mark.asyncio
async def test_state_q_is_6dp_fixpoint_of_mirror():
    mid, oids = await _seed_market(shares=("3.5", "0"), b=1.234567890123)
    await WRITER.start()
    st = WRITER.get_state(mid)
    assert st.outcome_ids == sorted(oids)           # 升序索引契约
    assert st.q_dec == [Decimal("3.500000"), Decimal("0.000000")]
    assert st.q == [float(Decimal("3.500000")), 0.0]
    assert len(st.prices) == 2
    assert abs(sum(st.prices) - 1.0) < 1e-9
    assert WRITER.market_id_for_outcome(oids[0]) == mid
    assert WRITER.market_id_for_outcome(999999) is None
    async def no_database(*args):
        raise AssertionError('warm LMSR input must come from the writer projection')
    trading_prices.REGISTRY._snapshot = no_database
    key = GroupKey(product='lmsr', id=mid)
    group = (await trading_prices.REGISTRY.read_groups([key]))[0]
    assert group.b == '1.234567890123'
    assert group.q_dec == ['3.500000', '0.000000']
    st.q_dec[0] = Decimal('99')
    group.q_dec[0] = '999'
    assert (await trading_prices.REGISTRY.read_groups([key]))[0].q_dec[0] == '3.500000'


@pytest.mark.asyncio
async def test_register_market_after_start():
    await WRITER.start()
    mid, oids = await _seed_market()
    assert WRITER.get_state(mid) is None
    key = GroupKey(product='lmsr', id=mid)
    with pytest.raises(HTTPException) as unavailable:
        await trading_prices.REGISTRY.read_groups([key])
    assert unavailable.value.status_code == 503
    async with async_session_maker() as db:
        await db.execute(update(Outcome).where(Outcome.id == oids[0]).values(total_shares=Decimal('9.123456')))
        await db.commit()
    await WRITER.register_market(mid)
    assert (await trading_prices.REGISTRY.read_groups([key]))[0].q_dec[0] == '9.123456'
    assert WRITER.get_state(mid) is not None
    assert WRITER.market_id_for_outcome(oids[0]) == mid


@pytest.mark.asyncio
async def test_stop_disables():
    mid, oids = await _seed_market()
    await WRITER.start()
    await WRITER.stop()
    assert not WRITER.enabled
    async with async_session_maker() as db:
        await db.execute(update(Outcome).where(Outcome.id == oids[0]).values(total_shares=Decimal('8.000001')))
        await db.commit()
    with pytest.raises(HTTPException) as unavailable:
        await trading_prices.REGISTRY.read_groups([GroupKey(product='lmsr', id=mid)])
    assert unavailable.value.status_code == 503
    await WRITER.start()
    group = (await trading_prices.REGISTRY.read_groups([GroupKey(product='lmsr', id=mid)]))[0]
    assert group.q_dec[0] == '8.000001'


@pytest.mark.asyncio
async def test_reload_installs_complete_source_and_failure_discards_old_projection(monkeypatch):
    from app.services import market_writer
    mid, oids = await _seed_market()
    await WRITER.start()
    clock = datetime(2030, 1, 1, tzinfo=timezone.utc)
    async with async_session_maker() as db:
        await db.execute(update(Market).where(Market.id == mid).values(
            liquidity_b=1.234567890123, status=MarketStatus.HALT, closes_at=clock))
        await db.execute(update(Outcome).where(Outcome.id == oids[0]).values(
            total_shares=Decimal('3.123456'), label='Updated'))
        await db.commit()
    await WRITER.reload_state(mid)
    state = WRITER.get_state(mid)
    assert state.b == 1.234567890123 and state.outcome_labels[0] == 'Updated'
    key = GroupKey(product='lmsr', id=mid)
    group = (await trading_prices.REGISTRY.read_groups([key]))[0]
    assert group.b == '1.234567890123' and group.q_dec[0] == '3.123456'
    assert group.status == MarketStatus.HALT and group.closes_at == clock.isoformat()
    async with async_session_maker() as db:
        await db.execute(update(Outcome).where(Outcome.id == oids[0]).values(total_shares=Decimal('7.654321')))
        await db.commit()
    async def fail_load(*args):
        raise RuntimeError('unavailable reload')
    with monkeypatch.context() as patch:
        patch.setattr(market_writer, '_load_one', fail_load)
        await WRITER.reload_state(mid)
        assert state.unavailable
    # Ordinary reads cannot recover an unavailable active writer via SQL.
    with pytest.raises(HTTPException) as unavailable:
        await trading_prices.REGISTRY.read_groups([key])
    assert unavailable.value.status_code == 503
    # External maintenance can recover an actor that rejects economic commands.
    await WRITER.reload_state(mid)
    assert not state.unavailable and state.q_dec[0] == Decimal('7.654321')
    assert (await trading_prices.REGISTRY.read_groups([key]))[0].q_dec[0] == '7.654321'
