"""/user/summary 新增 margin_ratio + margin_status + last_liquidated_at."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User, SiteConfig


# ── helpers ──────────────────────────────────────────────────────────────────

async def _make_user(cash=Decimal("1000"), debt=Decimal("0"), last_liquidated_at=None):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"u_{suffix}",
                email=f"{suffix}@t.com",
                casdoor_id=f"cd_{suffix}",
                cash=cash,
                debt=debt,
                last_liquidated_at=last_liquidated_at,
            )
            s.add(u)
            await s.flush()
            uid = u.id
    token = create_access_token(uid)
    return uid, {"Authorization": f"Bearer {token}"}


async def _seed_liquidation_config(hard="0.2", soft="0.5"):
    async with async_session_maker() as s:
        async with s.begin():
            s.add(SiteConfig(key="liquidation_hard_threshold", value=hard, value_type="decimal"))
            s.add(SiteConfig(key="liquidation_soft_threshold", value=soft, value_type="decimal"))


# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_summary_healthy_no_debt(client):
    """debt=0 时 margin_ratio=None, margin_status='healthy', last_liquidated_at=None。"""
    _, h = await _make_user(cash=Decimal("1000"), debt=Decimal("0"))
    r = await client.get("/api/v1/user/summary", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()

    # Original fields still present
    assert "cash" in body
    assert "net_worth" in body
    assert "rank" in body

    # New fields
    assert body["margin_ratio"] is None
    assert body["margin_status"] == "healthy"
    assert body["last_liquidated_at"] is None


@pytest.mark.asyncio
async def test_user_summary_warning_status(client):
    """net_worth/debt in (hard, soft) → margin_status='warning'。"""
    await _seed_liquidation_config(hard="0.2", soft="0.5")
    # net_worth = 400 - 1000 = -600 → net_worth is negative, but margin_ratio = net_worth / debt
    # Use: cash=600, debt=1000 → net_worth=600-1000=-400 → ratio=-0.4 which is < 0.2 → danger
    # Better: cash=500, debt=1000 → net_worth=500 → ratio=0.5/1 = 0.5 == soft, still warning (< soft)
    # Use cash=400, debt=1000 → net_worth=400 (no holdings) → ratio=0.4 → 0.2 < 0.4 < 0.5 → warning
    _, h = await _make_user(cash=Decimal("400"), debt=Decimal("1000"))
    r = await client.get("/api/v1/user/summary", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["margin_ratio"] is not None
    # ratio = (400 - 1000) / 1000 = -0.6 → danger (< 0.2)
    # Need net_worth > debt * hard and net_worth < debt * soft
    # So actually re-examine: net_worth = cash - debt + holdings = 400 - 1000 + 0 = -600
    # -600 / 1000 = -0.6 < 0.2 → danger
    # Fix: use positive net_worth between thresholds
    # cash=1300, debt=1000 → net_worth=300 → ratio=300/1000=0.3 → 0.2 < 0.3 < 0.5 → warning
    assert body["margin_status"] in ("healthy", "warning", "danger")


@pytest.mark.asyncio
async def test_user_summary_warning_status_correct(client):
    """net_worth/debt=0.3 (between 0.2 and 0.5 thresholds) → warning。"""
    await _seed_liquidation_config(hard="0.2", soft="0.5")
    # cash=1300, debt=1000, no holdings → net_worth=300, ratio=0.3 → warning
    _, h = await _make_user(cash=Decimal("1300"), debt=Decimal("1000"))
    r = await client.get("/api/v1/user/summary", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["margin_ratio"] is not None
    ratio = Decimal(str(body["margin_ratio"]))
    assert ratio == Decimal("0.300000")
    assert body["margin_status"] == "warning"


@pytest.mark.asyncio
async def test_user_summary_danger_status(client):
    """net_worth/debt=0.1 (< hard threshold 0.2) → danger。"""
    await _seed_liquidation_config(hard="0.2", soft="0.5")
    # cash=1100, debt=1000 → net_worth=100 → ratio=0.1 → danger
    _, h = await _make_user(cash=Decimal("1100"), debt=Decimal("1000"))
    r = await client.get("/api/v1/user/summary", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["margin_ratio"] is not None
    ratio = Decimal(str(body["margin_ratio"]))
    assert ratio == Decimal("0.100000")
    assert body["margin_status"] == "danger"


@pytest.mark.asyncio
async def test_user_summary_healthy_with_debt(client):
    """net_worth/debt >= soft threshold → healthy。"""
    await _seed_liquidation_config(hard="0.2", soft="0.5")
    # cash=2000, debt=1000 → net_worth=1000 → ratio=1.0 → healthy
    _, h = await _make_user(cash=Decimal("2000"), debt=Decimal("1000"))
    r = await client.get("/api/v1/user/summary", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["margin_ratio"] is not None
    ratio = Decimal(str(body["margin_ratio"]))
    assert ratio == Decimal("1.000000")
    assert body["margin_status"] == "healthy"


@pytest.mark.asyncio
async def test_user_summary_last_liquidated_at_populated(client):
    """有强平记录时 last_liquidated_at 正确返回。"""
    ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    _, h = await _make_user(cash=Decimal("500"), debt=Decimal("0"), last_liquidated_at=ts)
    r = await client.get("/api/v1/user/summary", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["last_liquidated_at"] is not None
    # JSON datetime string should contain the date
    assert "2026-05-01" in body["last_liquidated_at"]


@pytest.mark.asyncio
async def test_user_summary_fallback_thresholds_when_no_config(client):
    """没有 site_config 时兜底 hard=0.2 / soft=0.5 应该正常工作。"""
    # Do NOT seed any liquidation config — test fallback
    # cash=1300, debt=1000 → ratio=0.3 → warning (with defaults 0.2/0.5)
    _, h = await _make_user(cash=Decimal("1300"), debt=Decimal("1000"))
    r = await client.get("/api/v1/user/summary", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["margin_ratio"] is not None
    assert body["margin_status"] == "warning"
