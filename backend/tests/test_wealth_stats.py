"""平台资产统计：纯数学 + endpoint auth 测试。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.services.wealth_stats import (
    compute_wealth_distribution,
    gini_coefficient,
)


# ─── 纯数学 ─────────────────────────────────

def test_gini_empty_zero():
    assert gini_coefficient([]) == 0.0


def test_gini_all_equal_zero():
    assert gini_coefficient([100.0, 100.0, 100.0]) == 0.0


def test_gini_max_inequality_one_user_has_all():
    # 一个人占全部财富
    g = gini_coefficient([0.0, 0.0, 0.0, 100.0])
    # 理论上 (n-1)/n = 0.75；公式实现细节可能给 ~0.75
    assert 0.7 <= g <= 0.8


def test_gini_handles_negative_via_shift():
    # 有人欠钱，整体 shift 后算
    g = gini_coefficient([-100.0, 0.0, 100.0])
    assert 0.0 < g < 1.0


def test_compute_distribution_basic():
    result = compute_wealth_distribution(
        [100.0, 500.0, 1500.0, 3500.0, 12000.0, 40000.0],
        total_cash=20000.0, total_debt=0.0, total_holdings_value=37600.0,
    )
    assert result["user_count"] == 6
    assert result["min"] == 100.0
    assert result["max"] == 40000.0
    assert result["mean"] == round((100 + 500 + 1500 + 3500 + 12000 + 40000) / 6, 2)
    # 每档应该有 1 个用户
    counts = {b["label"]: b["count"] for b in result["brackets"]}
    assert counts == {
        "人类灵(已爆仓)": 1,
        "人里居民": 1,
        "天狗交易员": 1,
        "妖怪操盘手": 1,
        "炒炒币大亨": 1,
        "ZUN": 1,
    }


def test_compute_distribution_empty():
    result = compute_wealth_distribution(
        [], total_cash=0.0, total_debt=0.0, total_holdings_value=0.0,
    )
    assert result["user_count"] == 0
    assert result["gini"] == 0.0
    assert all(b["count"] == 0 for b in result["brackets"])


def test_compute_distribution_negative_net_worth():
    result = compute_wealth_distribution(
        [-200.0, 50.0, 500.0],
        total_cash=350.0, total_debt=200.0, total_holdings_value=200.0,
    )
    assert result["user_count"] == 3
    # -200 和 50 都进"人类灵"档（≤300）；500 进"人里居民"
    counts = {b["label"]: b["count"] for b in result["brackets"]}
    assert counts["人类灵(已爆仓)"] == 2
    assert counts["人里居民"] == 1


def test_compute_distribution_percentiles():
    # 10 个均匀分布的值
    values = [10.0 * i for i in range(1, 11)]  # 10, 20, ... 100
    result = compute_wealth_distribution(
        values, total_cash=550.0, total_debt=0.0, total_holdings_value=0.0,
    )
    # p50 应该约 55（10..100 的中位）
    assert 50 <= result["p50"] <= 60


# ─── Endpoint auth ─────────────────────────

async def _seed_user(is_superuser=False, cash=Decimal("100")) -> tuple[int, dict]:
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as s:
        u = User(
            username=f"u_{suffix}",
            email=f"{suffix}@t.com",
            casdoor_id=f"cd_{suffix}",
            cash=cash, is_superuser=is_superuser,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id, {"Authorization": f"Bearer {create_access_token(u.id)}"}


@pytest.mark.asyncio
async def test_endpoint_returns_stats_for_admin(client):
    _, admin_h = await _seed_user(is_superuser=True, cash=Decimal("5000"))
    await _seed_user(cash=Decimal("100"))
    await _seed_user(cash=Decimal("1500"))

    resp = await client.get("/api/v1/admin/stats/wealth", headers=admin_h)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["user_count"] == 3
    assert data["mean"] > 0
    assert "brackets" in data
    assert len(data["brackets"]) == 6


@pytest.mark.asyncio
async def test_endpoint_forbids_normal_user(client):
    _, h = await _seed_user(is_superuser=False)
    resp = await client.get("/api/v1/admin/stats/wealth", headers=h)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_endpoint_rejects_unauthenticated(client):
    resp = await client.get("/api/v1/admin/stats/wealth")
    assert resp.status_code in (401, 403)
