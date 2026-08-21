"""MarketWriter 状态加载/注册/重置单元测试（不走 HTTP，不需要 client fixture）。"""
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlmodel import SQLModel

from app.core.database import engine, async_session_maker
from app.models.base import Market, MarketStatus, Outcome
from app.services.market_writer import WRITER


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    yield
    await WRITER.stop()


async def _seed_market(status=MarketStatus.TRADING, shares=("3.5", "0")) -> tuple[int, list[int]]:
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=status)
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


@pytest.mark.asyncio
async def test_state_q_is_6dp_fixpoint_of_mirror():
    mid, oids = await _seed_market(shares=("3.5", "0"))
    await WRITER.start()
    st = WRITER.get_state(mid)
    assert st.outcome_ids == sorted(oids)           # 升序索引契约
    assert st.q_dec == [Decimal("3.500000"), Decimal("0.000000")]
    assert st.q == [float(Decimal("3.500000")), 0.0]
    assert len(st.prices) == 2
    assert abs(sum(st.prices) - 1.0) < 1e-9
    assert WRITER.market_id_for_outcome(oids[0]) == mid
    assert WRITER.market_id_for_outcome(999999) is None


@pytest.mark.asyncio
async def test_register_market_after_start():
    await WRITER.start()
    mid, oids = await _seed_market()
    assert WRITER.get_state(mid) is None
    await WRITER.register_market(mid)
    assert WRITER.get_state(mid) is not None
    assert WRITER.market_id_for_outcome(oids[0]) == mid


@pytest.mark.asyncio
async def test_stop_disables():
    await WRITER.start()
    await WRITER.stop()
    assert not WRITER.enabled
