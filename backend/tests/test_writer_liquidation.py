"""LiquidateMarketCmd op + liquidate_user_split 编排器单元测试（spec § 4.6）。"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.core.database import async_session_maker
from app.models.base import (
    Market, MarketStatus, Outcome, Position, Transaction, TransactionType, User,
)
from app.services.market_writer import WRITER


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    yield
    await WRITER.stop()


async def _seed_market(status=MarketStatus.TRADING, shares=("3.5", "0")) -> tuple[int, list[int]]:
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0, status=status, tags="")
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


async def _seed_debtor(cash="0", debt="50") -> int:
    async with async_session_maker() as s:
        u = User(username="debtor", casdoor_id="cas_debtor",
                 cash=Decimal(cash), debt=Decimal(debt), is_active=True,
                 debt_last_accrued_at=datetime.now(timezone.utc))
        s.add(u); await s.flush()
        uid = u.id
        await s.commit()
        return uid


async def _give_position(uid, oid, amount, cost):
    """直接写 Position + 同步 outcome 镜像（绕过交易，测试布景用）。"""
    async with async_session_maker() as s:
        s.add(Position(user_id=uid, outcome_id=oid,
                       amount=Decimal(amount), cost_basis=Decimal(cost)))
        await s.execute(update(Outcome).where(Outcome.id == oid)
                        .values(total_shares=Outcome.total_shares + Decimal(amount)))
        await s.commit()


ARGS = dict(daily_rate=Decimal("0.001"), trigger_source="test",
            partial_pct=Decimal("1"), target_margin=Decimal("0.5"),
            emergency_threshold=Decimal("0.1"),
            hard_threshold=Decimal("1.0"))


@pytest.mark.asyncio
async def test_split_liquidation_two_markets_writes_one_summary_event():
    from app.models.base import LiquidationEvent
    from app.services.liquidation_service import liquidate_user_split
    m1, o1 = await _seed_market(shares=("0", "0"))
    m2, o2 = await _seed_market(shares=("0", "0"))
    uid = await _seed_debtor(cash="0", debt="50")
    await _give_position(uid, o1[0], "20", "10")
    await _give_position(uid, o2[0], "20", "10")
    await WRITER.start()
    ev = await liquidate_user_split(uid, **ARGS)
    assert ev is not None
    assert ev.sold_positions_count == 2
    assert ev.total_proceeds > 0
    assert ev.repaid_amount > 0
    async with async_session_maker() as s:
        assert (await s.execute(select(Position))).scalars().first() is None
        events = (await s.execute(select(LiquidationEvent))).scalars().all()
        assert len(events) == 1                        # 汇总一条，不是每市场一条
        liq_tx = (await s.execute(select(Transaction).where(
            Transaction.type == TransactionType.LIQUIDATE))).scalars().all()
        assert len(liq_tx) == 2
        u = await s.get(User, uid)
        assert u.last_liquidated_at is not None
    # 内存镜像同步
    assert WRITER.get_state(m1).q_dec[0] == Decimal("0.000000")
    assert WRITER.get_state(m2).q_dec[0] == Decimal("0.000000")


@pytest.mark.asyncio
async def test_split_liquidation_user_with_halt_holdings_skipped_entirely():
    """阶段 A 锁内复检：持有任何 HALT 仓的用户整体放过（与老路径 sweep 守卫同语义）。"""
    from app.models.base import LiquidationEvent
    from app.services.liquidation_service import liquidate_user_split
    m1, o1 = await _seed_market(shares=("0", "0"))
    m2, o2 = await _seed_market(shares=("0", "0"), status=MarketStatus.HALT)
    uid = await _seed_debtor()
    await _give_position(uid, o1[0], "20", "10")
    await _give_position(uid, o2[0], "20", "10")
    await WRITER.start()
    ev = await liquidate_user_split(uid, **ARGS)
    assert ev == "halt"                                # 复检放过：返回原因串，不冷却
    async with async_session_maker() as s:
        remaining = (await s.execute(select(Position))).scalars().all()
        assert len(remaining) == 2                     # 一张仓都没动
        assert (await s.execute(select(LiquidationEvent))).scalars().first() is None


@pytest.mark.asyncio
async def test_split_liquidation_recheck_margin_recovered_returns_none():
    """阶段 A 锁内复检：判定窗口里用户已自救（margin ≥ hard）→ 整体放过（IMP-3 修复）。"""
    from app.models.base import LiquidationEvent
    from app.services.liquidation_service import liquidate_user_split
    m1, o1 = await _seed_market(shares=("0", "0"))
    uid = await _seed_debtor(cash="1000", debt="50")   # margin ≈ 19 >> hard=1.0
    await _give_position(uid, o1[0], "20", "10")
    await WRITER.start()
    ev = await liquidate_user_split(uid, **ARGS)
    assert ev == "recovered"                           # 复检放过：返回原因串，不冷却
    async with async_session_maker() as s:
        remaining = (await s.execute(select(Position))).scalars().all()
        assert len(remaining) == 1                     # 仓位原封不动
        assert (await s.execute(select(LiquidationEvent))).scalars().first() is None
        assert (await s.get(User, uid)).last_liquidated_at is None


@pytest.mark.asyncio
async def test_op_liquidate_market_halt_returns_empty():
    """op 级 HALT 防御（阶段 A 通过后、阶段 B 执行前市场被熔断的 race 兜底）。"""
    from app.services.writer_ops import LiquidateMarketCmd
    m1, o1 = await _seed_market(shares=("0", "0"), status=MarketStatus.HALT)
    uid = await _seed_debtor()
    await _give_position(uid, o1[0], "20", "10")
    await WRITER.start()
    r = await WRITER.submit(LiquidateMarketCmd(
        market_id=m1, user_id=uid, mode="emergency", partial_pct=Decimal("1")))
    assert r == {"sold_count": 0, "total_proceeds": Decimal("0")}
    async with async_session_maker() as s:
        remaining = (await s.execute(select(Position))).scalars().all()
        assert len(remaining) == 1


@pytest.mark.asyncio
async def test_split_liquidation_noop_returns_none_writes_nothing():
    from app.models.base import LiquidationEvent
    from app.services.liquidation_service import liquidate_user_split
    uid = await _seed_debtor(cash="0", debt="50")       # 无持仓无现金
    await WRITER.start()
    ev = await liquidate_user_split(uid, **ARGS)
    assert ev is None
    async with async_session_maker() as s:
        assert (await s.execute(select(LiquidationEvent))).scalars().first() is None
        assert (await s.get(User, uid)).last_liquidated_at is None
