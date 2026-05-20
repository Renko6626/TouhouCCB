"""/admin/liquidation/run-now 测试。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import (
    LiquidationEvent, Market, MarketStatus, Outcome,
    Position, SiteConfig, User,
)


@pytest_asyncio.fixture(autouse=True)
async def _seed_liquidation_config(setup_db):
    """conftest 的 setup_db 已 drop+create；此 fixture 仅追加 SiteConfig 种子。"""
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key="liquidation_enabled", value="false", value_type="bool"))
            s.add(SiteConfig(key="liquidation_hard_threshold", value="0.2", value_type="decimal"))
            s.add(SiteConfig(key="liquidation_soft_threshold", value="0.5", value_type="decimal"))
            s.add(SiteConfig(key="loan_daily_rate", value="0.01", value_type="decimal"))
            s.add(SiteConfig(key="liquidation_sweep_interval_sec", value="600", value_type="int"))
            s.add(SiteConfig(key="liquidation_partial_pct", value="0.10", value_type="decimal"))
            s.add(SiteConfig(key="liquidation_target_margin", value="0.30", value_type="decimal"))
            s.add(SiteConfig(key="liquidation_emergency_threshold", value="0.05", value_type="decimal"))


async def _seed_user(is_superuser=False) -> tuple[int, dict]:
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as s:
        u = User(
            username=f"u_{suffix}",
            email=f"{suffix}@t.com",
            casdoor_id=f"cd_{suffix}",
            cash=Decimal("100"),
            is_superuser=is_superuser,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id, {"Authorization": f"Bearer {create_access_token(u.id)}"}


@pytest.mark.asyncio
async def test_admin_run_now_unauthorized(client):
    """无 token → 401/403。"""
    resp = await client.post("/api/v1/admin/liquidation/run-now")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_admin_run_now_normal_user_forbidden(client):
    """普通用户 token → 403。"""
    _, headers = await _seed_user(is_superuser=False)
    resp = await client.post("/api/v1/admin/liquidation/run-now", headers=headers)
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_admin_run_now_returns_sweep_result(client):
    """admin 调用应返回 sweep 结果 dict（disabled 时含 skipped）。"""
    _, admin_headers = await _seed_user(is_superuser=True)
    resp = await client.post("/api/v1/admin/liquidation/run-now", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    j = resp.json()
    # liquidation_enabled 默认 false → skipped: "disabled"，或若 enabled 则有 triggered_count
    assert "skipped" in j or "triggered_count" in j


@pytest.mark.asyncio
async def test_admin_run_now_trigger_source_is_admin_manual(client):
    """admin 端点触发的 LiquidationEvent.trigger_source 应为 'admin_manual'。"""
    from app.services.site_config import clear_cache

    # 先开启 liquidation 功能
    async with async_session_maker() as s:
        async with s.begin():
            row = (await s.execute(
                select(SiteConfig).where(SiteConfig.key == "liquidation_enabled")
            )).scalars().first()
            row.value = "true"
    clear_cache()

    # 插入一个保证金严重不足的用户（cash=0, debt=1000, hv≈0 → margin 极低 << 0.2）
    async with async_session_maker() as s:
        async with s.begin():
            victim = User(
                username="trigger_src_victim",
                casdoor_id="trigger_src_cd",
                cash=Decimal("0"),
                debt=Decimal("1000"),
                debt_last_accrued_at=datetime.now(timezone.utc),
            )
            s.add(victim)
            await s.flush()

            m = Market(
                title="m_trigger",
                description="",
                liquidity_b=100.0,
                status=MarketStatus.TRADING,
                tags="",
            )
            s.add(m)
            await s.flush()

            # 两个 outcome，其中 A 有持仓
            o_a = Outcome(market_id=m.id, label="A", total_shares=Decimal("150"))
            o_b = Outcome(market_id=m.id, label="B", total_shares=Decimal("50"))
            s.add(o_a)
            s.add(o_b)
            await s.flush()

            p = Position(
                user_id=victim.id,
                outcome_id=o_a.id,
                amount=Decimal("100"),
                cost_basis=Decimal("0"),
            )
            s.add(p)
            victim_id = victim.id

    _, admin_headers = await _seed_user(is_superuser=True)
    resp = await client.post("/api/v1/admin/liquidation/run-now", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j.get("triggered_count", 0) >= 1, f"应触发强平, got {j}"

    # 验证 LiquidationEvent.trigger_source == "admin_manual"
    async with async_session_maker() as s:
        events = (await s.execute(
            select(LiquidationEvent).where(LiquidationEvent.user_id == victim_id)
        )).scalars().all()
        assert len(events) == 1, f"应有 1 笔 LiquidationEvent, got {len(events)}"
        assert events[0].trigger_source == "admin_manual", (
            f"expected 'admin_manual', got '{events[0].trigger_source}'"
        )
