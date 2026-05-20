import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timezone
from decimal import Decimal
import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import (
    LiquidationEvent, Market, MarketStatus, Outcome,
    Position, SiteConfig, User,
)
from app.services.market_locks import lock_user
from app.services import liquidation_service, liquidation_sweep, site_config


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
    """partial_pct=1.0 在 partial mode 路径下退化为全卖 (即 spec § Rollback 承诺)。

    review I-1 修：必须用 margin > emergency_threshold 的 setup（否则走 emergency
    全卖路径，无法证明 partial_pct=1.0 fall-through 真的工作）。
    setup: cash=200, debt=150, share=100 → margin=1.0 → mode='partial' → partial_pct=1.0
    → sell_amount = pos.amount × 1.0 = pos.amount → sell_amount >= pos.amount → delete。
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
                partial_pct=Decimal("1.0"),
                target_margin=Decimal("0.3"),
                emergency_threshold=Decimal("0.05"),
            )
        # 关键：必须是 partial mode 走到的全卖，不是 emergency 路径
        assert ev.mode == "partial", (
            f"setup margin>>emergency_threshold 应走 partial, 实际 {ev.mode}"
        )

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Position).where(Position.user_id == uid)
        )).scalars().all()
        assert len(rows) == 0, "partial_pct=1.0 应等同全卖删除 position"


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
async def test_partial_ceil_forces_full_sell_on_fractional_position(client):
    """小数持仓在 partial 模式下被 ceil 取整后超过 pos.amount → 触发全卖边界清掉。

    旧逻辑 (quantize 6 位): amount=0.000005 × 0.1 = 0.0000005 → 量化 6 位 = 0 → skip
    新逻辑 (ROUND_CEILING 整数): ceil(0.0000005) = 1 > 0.000005 → clamp 到 pos.amount → 全卖
    设计意图：partial_pct 算出来的 sell_amount 取整到整数股 (玩家体感"卖 X 股不是 0.5 股")，
    零碎小数持仓一波清掉，避免长期挂着。
    """
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
                emergency_threshold=Decimal("-999"),  # 强制走 partial 路径
            )
        assert ev.mode == "partial"
        # ceil 后 sell_amount=1 >> pos.amount=0.000005 → clamp 到 pos.amount → 全卖
        assert ev.sold_positions_count == 1

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Position).where(Position.user_id == uid)
        )).scalars().all()
        assert len(rows) == 0, "小数持仓被 ceil 强制全卖，应删除 position"


# ─── T7: sweep 集成测 ─────────────────────────────────────────────────────────

async def _seed_sweep_site_config(*, partial_pct: str = "0.5"):
    """sweep 跑前 INSERT 全部需要的 site_config 行（setup_db 清掉之后）。

    partial_pct 测试默认 0.5（收敛快）；测 emergency 路径时无所谓。
    """
    async with async_session_maker() as s:
        async with s.begin():
            for k, v, t in [
                ("liquidation_enabled", "true", "bool"),
                ("liquidation_hard_threshold", "0.2", "decimal"),
                ("liquidation_soft_threshold", "0.5", "decimal"),
                ("liquidation_sweep_interval_sec", "600", "int"),
                ("loan_daily_rate", "0.001", "decimal"),
                ("liquidation_partial_pct", partial_pct, "decimal"),
                ("liquidation_target_margin", "0.30", "decimal"),
                ("liquidation_emergency_threshold", "0.05", "decimal"),
            ]:
                s.add(SiteConfig(key=k, value=v, value_type=t))
    site_config.clear_cache()


@pytest.mark.asyncio
async def test_sweep_partial_then_converges_after_multiple_ticks(client):
    """多 tick partial → debt 减少 + 所有 events mode='partial'。

    setup: cash=0, debt=180, share=200, market=200 (单 outcome LMSR LCV≈200)
      → NW=20, margin=20/180=0.111 ∈ [0.05, 0.20) → partial 路径 ✓
    partial_pct=0.1（小幅，避免一波就把 debt 还清，强制多 tick 收敛）。
    每 tick: sell 10% of shares → 还债 → margin 缓慢上升直到 ≥ hard_threshold。
    """
    await _seed_sweep_site_config(partial_pct="0.1")
    liquidation_sweep._recently_attempted.clear()

    uid, mid, oid = await _setup_user(
        cash=0, debt=180, share_amount=200, market_total_shares=200,
    )

    result1 = await liquidation_sweep.run_liquidation_sweep_once()
    assert result1["triggered_count"] >= 1, f"第 1 tick 应触发: {result1}"

    async with async_session_maker() as db:
        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == uid)
        )).scalars().all()
        assert len(events) == 1, f"第 1 tick 应写 1 event, 实际 {len(events)}"
        assert events[0].mode == "partial", f"应 partial, 实际 {events[0].mode}"

    # 多 tick 直到收敛 (清 cooldown + 进程缓存)
    for _ in range(5):
        liquidation_sweep._recently_attempted.clear()
        site_config.clear_cache()
        await liquidation_sweep.run_liquidation_sweep_once()

    async with async_session_maker() as db:
        u = await db.get(User, uid)
        assert u.debt < Decimal("180"), f"debt 应减少, 实际 {u.debt}"

        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == uid)
        )).scalars().all()
        assert len(events) >= 2, f"至少 2 波 partial, 实际 {len(events)}"
        modes = [e.mode for e in events]
        assert all(m == "partial" for m in modes), (
            f"全部应 partial, 实际 {modes}"
        )

    # review I-2: 真正验证收敛——多 tick 后 margin 应 ≥ hard_threshold (0.2)，
    # 再跑一次 sweep 应 triggered_count==0 (用户已脱离 hard 门槛)。
    liquidation_sweep._recently_attempted.clear()
    site_config.clear_cache()
    final_result = await liquidation_sweep.run_liquidation_sweep_once()
    assert final_result["triggered_count"] == 0, (
        f"6 ticks partial 之后应已收敛过 hard_threshold, "
        f"但 sweep 还在触发: {final_result}"
    )


@pytest.mark.asyncio
async def test_emergency_mode_written_when_severe(client):
    """user margin << emergency_threshold → mode='emergency' 写入 event."""
    await _seed_sweep_site_config(partial_pct="0.1")
    liquidation_sweep._recently_attempted.clear()

    # cash=0, debt=1000, share=10 → LCV≈10, NW=-990, margin=-0.99 << 0.05 → emergency
    uid, mid, oid = await _setup_user(
        cash=0, debt=1000, share_amount=10, market_total_shares=10,
    )

    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result["triggered_count"] >= 1, f"应触发: {result}"

    async with async_session_maker() as db:
        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == uid)
        )).scalars().all()
        assert len(events) >= 1
        assert events[0].mode == "emergency", (
            f"应 emergency, 实际 {events[0].mode}"
        )
