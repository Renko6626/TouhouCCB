"""/admin/liquidation/run-now 测试。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import SiteConfig, User


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
