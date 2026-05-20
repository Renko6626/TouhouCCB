import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timezone
from decimal import Decimal
import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import (
    LiquidationEvent, Market, MarketStatus, Outcome,
    Position, User,
)
from app.services.market_locks import lock_user
from app.services import liquidation_service


async def _setup_user(*, cash, debt, share_amount, market_total_shares):
    """造 1 user + 1 market + 1 outcome + 1 position。

    返回 (user_id, market_id, outcome_id)。
    """
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"part_{cash}_{debt}",
                casdoor_id=f"part_cas_{cash}_{debt}",
                cash=Decimal(str(cash)),
                debt=Decimal(str(debt)),
                debt_last_accrued_at=(
                    datetime.now(timezone.utc) if debt > 0 else None
                ),
            )
            s.add(u)
            await s.flush()

            m = Market(
                title="part_test", description="", liquidity_b=100.0,
                status=MarketStatus.TRADING, tags="",
            )
            s.add(m)
            await s.flush()

            o = Outcome(
                market_id=m.id, label="A",
                total_shares=Decimal(str(market_total_shares)),
            )
            s.add(o)
            await s.flush()

            p = Position(
                user_id=u.id, outcome_id=o.id,
                amount=Decimal(str(share_amount)),
                cost_basis=Decimal(str(share_amount)) * Decimal("0.5"),  # avg_price = 0.5
            )
            s.add(p)
            return u.id, m.id, o.id


@pytest.mark.asyncio
async def test_partial_50pct_sells_half_and_updates_cost_basis(client):
    """partial_pct=0.5 → 卖一半，cost_basis 也减一半，avg_price 不变。

    setup margin 需要 > emergency_threshold (0.05) 才走 partial 路径。
    单 outcome market LMSR LCV ≈ shares，所以 cash=200/debt=150 让
    NW=200+100-150=150, margin=1.0 → partial 路径 ✓。
    """
    uid, mid, oid = await _setup_user(
        cash=200, debt=150, share_amount=100, market_total_shares=100,
    )

    async with async_session_maker() as db:
        async with db.begin():
            user = await lock_user(db, uid)
            ev = await liquidation_service.liquidate_user(
                db, user, daily_rate=Decimal("0.001"),
                trigger_source="scheduler",
                partial_pct=Decimal("0.5"),
                target_margin=Decimal("0.3"),
                emergency_threshold=Decimal("0.05"),
            )
        assert ev.mode == "partial"
        assert ev.sold_positions_count == 1

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Position).where(Position.user_id == uid)
        )).scalars().all()
        assert len(rows) == 1, "partial 不应删除 position"
        p = rows[0]
        assert p.amount == Decimal("50"), f"amount 应=50, 实际 {p.amount}"
        # cost_basis 25 (= 100 * 0.5 * 50%)
        assert p.cost_basis == Decimal("25"), f"cost_basis 应=25, 实际 {p.cost_basis}"
        # avg_price 不变 (25 / 50 = 0.5 ≈ 原 50/100)
        assert (p.cost_basis / p.amount) == Decimal("0.5")


@pytest.mark.asyncio
async def test_partial_full_pct_acts_like_emergency_all_in(client):
    """partial_pct=1.0 → 退化为全平 (position 被删除)。"""
    uid, mid, oid = await _setup_user(
        cash=100, debt=300, share_amount=100, market_total_shares=100,
    )

    async with async_session_maker() as db:
        async with db.begin():
            user = await lock_user(db, uid)
            ev = await liquidation_service.liquidate_user(
                db, user, daily_rate=Decimal("0.001"),
                trigger_source="scheduler",
                partial_pct=Decimal("1.0"),
                target_margin=Decimal("0.3"),
                emergency_threshold=Decimal("0.05"),
            )

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Position).where(Position.user_id == uid)
        )).scalars().all()
        assert len(rows) == 0, "partial_pct=1.0 应等同 emergency 删除 position"


@pytest.mark.asyncio
async def test_emergency_mode_when_pre_margin_below_threshold(client):
    """pre_margin < emergency_threshold (0.05) → mode='emergency' + 全平
    即使传 partial_pct=0.1 也走 emergency 路径。"""
    # cash=0, debt=1000, shares=10, market=10
    # LMSR: sell 10/10 → LCV ≈ 5
    # pre_margin = (0 + 5 - 1000) / 1000 ≈ -0.995 << 0.05 → emergency
    uid, mid, oid = await _setup_user(
        cash=0, debt=1000, share_amount=10, market_total_shares=10,
    )

    async with async_session_maker() as db:
        async with db.begin():
            user = await lock_user(db, uid)
            ev = await liquidation_service.liquidate_user(
                db, user, daily_rate=Decimal("0.001"),
                trigger_source="scheduler",
                partial_pct=Decimal("0.1"),  # 即使传 partial，应被 emergency 覆盖
                target_margin=Decimal("0.3"),
                emergency_threshold=Decimal("0.05"),
            )
        assert ev.mode == "emergency"

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Position).where(Position.user_id == uid)
        )).scalars().all()
        assert len(rows) == 0, "emergency 全平应删 position"


@pytest.mark.asyncio
async def test_partial_sell_amount_quantized_to_6_digits(client):
    """partial_pct × amount 量化到 6 位小数；过小被跳过不报错。"""
    # amount=0.000005, partial_pct=0.1 → 0.0000005，量化到 6 位 = 0 → skip
    # 但 user.debt 需要 > 0 才会跑 liquidate_user.
    # margin 算法依赖 LCV: 太小 LCV 趋近 0
    # cash=10, debt=100 → margin = (10 + LCV - 100) / 100
    # LCV(0.000005 shares) ≈ 0 → margin ≈ -0.9 → emergency 路径
    # 我们想让 partial 路径走到 sell_amount = 0 → 需要 mode=partial
    # 改 cash=200, debt=100, shares=0.000005 → LCV≈0 → margin ≈ 1.0 不触发?
    # 但 liquidate_user 不直接判 should-trigger，调用方判 hard_thr
    # 我们直接调 liquidate_user，传 emergency_thr=0 让一定走 partial
    uid, mid, oid = await _setup_user(
        cash=10, debt=100,
        share_amount=Decimal("0.000005"),
        market_total_shares=Decimal("0.000005"),
    )

    async with async_session_maker() as db:
        async with db.begin():
            user = await lock_user(db, uid)
            ev = await liquidation_service.liquidate_user(
                db, user, daily_rate=Decimal("0.001"),
                trigger_source="scheduler",
                partial_pct=Decimal("0.1"),
                target_margin=Decimal("0.3"),
                emergency_threshold=Decimal("-999"),  # 强制 partial mode
            )
        assert ev.mode == "partial"
        # sell_amount 量化 0 → skip
        assert ev.sold_positions_count == 0

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Position).where(Position.user_id == uid)
        )).scalars().all()
        # position 不应被删（partial skip）
        assert len(rows) == 1
        assert rows[0].amount == Decimal("0.000005")  # 没动
