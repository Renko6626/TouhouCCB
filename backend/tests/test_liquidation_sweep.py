"""liquidation_sweep.run_liquidation_sweep_once 单元测：阈值筛 user + 防爆 cache + deadlock retry。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import (
    Market, MarketStatus, Outcome, Position, SiteConfig, User, LiquidationEvent,
)
from app.services import liquidation_sweep


# ---------------------------------------------------------------------------
# module-level seed fixture: insert all site_config keys needed by sweep
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_recently_attempted():
    """每个测试前清掉 module-level 防爆 cache 和 site_config 进程缓存。"""
    from app.services.site_config import clear_cache
    liquidation_sweep._recently_attempted.clear()
    clear_cache()
    yield
    liquidation_sweep._recently_attempted.clear()
    clear_cache()


@pytest_asyncio.fixture(autouse=True)
async def _seed_base_config(setup_db):
    """每个测试 drop+create 之后插入 sweep 需要的 site_config 行（默认 disabled）。"""
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key="liquidation_enabled", value="false", value_type="bool"))
            s.add(SiteConfig(key="liquidation_hard_threshold", value="0.2", value_type="decimal"))
            s.add(SiteConfig(key="liquidation_soft_threshold", value="0.5", value_type="decimal"))
            s.add(SiteConfig(key="loan_daily_rate", value="0.01", value_type="decimal"))
            s.add(SiteConfig(key="liquidation_sweep_interval_sec", value="600", value_type="int"))


async def _enable_liquidation():
    """Helper: flip liquidation_enabled → true (直接更新 DB 行，绕过 set_value 权限检查)。"""
    from app.services.site_config import clear_cache
    async with async_session_maker() as s:
        async with s.begin():
            row = (await s.execute(
                select(SiteConfig).where(SiteConfig.key == "liquidation_enabled")
            )).scalars().first()
            row.value = "true"
    clear_cache()


async def _seed_user(db, *, cash, debt, hv_via_position=None):
    """hv_via_position: (label, amount) 或 None. 返回 user_id。"""
    u = User(username=f"u{cash}_{debt}", casdoor_id=f"cas_{cash}_{debt}",
             cash=Decimal(str(cash)), debt=Decimal(str(debt)),
             debt_last_accrued_at=datetime.now(timezone.utc) if debt > 0 else None)
    db.add(u)
    await db.flush()

    if hv_via_position is not None:
        label, amount = hv_via_position
        m = Market(title=f"m_{u.id}", description="", liquidity_b=100.0,
                   status=MarketStatus.TRADING, tags="")
        db.add(m)
        await db.flush()
        o = Outcome(market_id=m.id, label=label, total_shares=Decimal(str(amount + 50)))
        db.add(o)
        await db.flush()
        p = Position(user_id=u.id, outcome_id=o.id,
                     amount=Decimal(str(amount)), cost_basis=Decimal("0"))
        db.add(p)

    await db.commit()
    return u.id


@pytest.mark.asyncio
async def test_sweep_skips_when_disabled(client):
    """liquidation_enabled=false（default）时整体 skip。"""
    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result.get("skipped") == "disabled"


@pytest.mark.asyncio
async def test_sweep_skips_user_with_no_debt(client):
    """debt=0 的 user 不应被选中。"""
    await _enable_liquidation()

    async with async_session_maker() as db:
        await _seed_user(db, cash=100, debt=0)

    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result["triggered_count"] == 0


@pytest.mark.asyncio
async def test_sweep_triggers_user_below_hard_threshold(client):
    """user margin < 0.2 应触发强平。"""
    await _enable_liquidation()

    async with async_session_maker() as db:
        # cash=0, debt=500, hv≈50 → NW=-450, margin=-0.9 → far below 0.2
        uid = await _seed_user(db, cash=0, debt=500, hv_via_position=("A", 50))

    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result["triggered_count"] == 1, f"expected 1 trigger, got {result}"

    async with async_session_maker() as db:
        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == uid)
        )).scalars().all()
        assert len(events) == 1


@pytest.mark.asyncio
async def test_sweep_recently_attempted_cache_skips(client):
    """已扫过但没产生进展（资不抵债 cash=0+hv=0）的 user 30 min 内不重扫。"""
    await _enable_liquidation()

    async with async_session_maker() as db:
        # 0 cash, 1000 debt, 0 holdings = stuck underwater
        uid = await _seed_user(db, cash=0, debt=1000)

    # 第一次跑：触发，但 sold=0 + repaid=0 → 标记 stuck
    result1 = await liquidation_sweep.run_liquidation_sweep_once()
    assert uid in liquidation_sweep._recently_attempted

    # 第二次立即跑：被 cache 跳过，triggered_count=0
    result2 = await liquidation_sweep.run_liquidation_sweep_once()
    assert result2["triggered_count"] == 0


@pytest.mark.asyncio
async def test_sweep_reports_sweep_duration_ms(client):
    """sweep 返回 sweep_duration_ms 字段。"""
    await _enable_liquidation()
    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert "sweep_duration_ms" in result


@pytest.mark.asyncio
async def test_sweep_skips_user_with_halt_holdings(client):
    """流动性危机保护：用户在 HALT 市场有持仓时，即使 LCV margin 危险也跳过。

    场景：用户同时持有 TRADING 市场（LCV 算入）和 HALT 市场（LCV=0）的仓位。
    LCV margin 看起来危险 → 但 HALT 持仓可能价值高，市场恢复后用户其实健康。
    sweep 应保守跳过本轮，等市场恢复 TRADING 后再判定。
    """
    await _enable_liquidation()

    async with async_session_maker() as db:
        # 造一个 user，cash=0 debt=500，只有 HALT 市场持仓
        u = User(
            username="halt_protected", casdoor_id="cas_halt",
            cash=Decimal("0"), debt=Decimal("500"),
            debt_last_accrued_at=datetime.now(timezone.utc),
        )
        db.add(u)
        await db.flush()

        m = Market(
            title="m_halt", description="", liquidity_b=100.0,
            status=MarketStatus.HALT, tags="",
        )
        db.add(m)
        await db.flush()
        o = Outcome(market_id=m.id, label="A", total_shares=Decimal("100"))
        db.add(o)
        await db.flush()
        p = Position(
            user_id=u.id, outcome_id=o.id,
            amount=Decimal("100"), cost_basis=Decimal("80"),
        )
        db.add(p)
        await db.commit()
        uid = u.id

    result = await liquidation_sweep.run_liquidation_sweep_once()

    # 应跳过：triggered_count=0、不写 LiquidationEvent
    assert result["triggered_count"] == 0, (
        f"HALT 持仓用户应被守卫跳过，但被强平了: {result}"
    )
    async with async_session_maker() as db:
        events = (await db.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == uid)
        )).scalars().all()
        assert len(events) == 0, "HALT 守卫触发时不应写 LiquidationEvent"


@pytest.mark.asyncio
async def test_sweep_triggers_user_with_only_trading_holdings(client):
    """对照测试：只有 TRADING 持仓的用户 margin 危险时仍正常强平。

    确认 HALT 守卫只豁免有 HALT 持仓的用户，不影响正常强平路径。
    """
    await _enable_liquidation()

    async with async_session_maker() as db:
        uid = await _seed_user(
            db, cash=0, debt=500, hv_via_position=("A", 50),
        )

    result = await liquidation_sweep.run_liquidation_sweep_once()
    assert result["triggered_count"] == 1, (
        f"纯 TRADING 持仓用户应被正常强平，但被跳过了: {result}"
    )
    assert isinstance(result["sweep_duration_ms"], int)
