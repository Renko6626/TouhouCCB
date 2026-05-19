"""/recent-liquidations 公开端点测试。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import LiquidationEvent, User


@pytest.mark.asyncio
async def test_recent_liquidations_anonymous_ok(client):
    """匿名（不带 token）应能访问。"""
    resp = await client.get("/api/v1/loan/recent-liquidations?limit=5")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_recent_liquidations_returns_fields(client):
    """字段完整 + username 来自 join。"""
    async with async_session_maker() as db:
        u = User(username="liqtest_pub", casdoor_id="liqpub",
                 cash=Decimal("0"), debt=Decimal("100"))
        db.add(u); await db.flush()
        ev = LiquidationEvent(
            user_id=u.id,
            triggered_at=datetime.now(timezone.utc),
            pre_cash=Decimal("0"), pre_debt=Decimal("100"),
            pre_holdings_value=Decimal("50"),
            pre_net_worth=Decimal("-50"),
            pre_margin_ratio=Decimal("-0.5"),
            sold_positions_count=1,
            total_proceeds=Decimal("40"),
            repaid_amount=Decimal("40"),
            remaining_debt=Decimal("60"),
            post_cash=Decimal("0"),
            trigger_source="scheduler",
        )
        db.add(ev); await db.commit()

    resp = await client.get("/api/v1/loan/recent-liquidations?limit=20")
    rows = resp.json()
    assert len(rows) >= 1
    row = next(r for r in rows if r["username"] == "liqtest_pub")
    assert "triggered_at" in row
    assert "pre_debt" in row
    assert "pre_net_worth" in row
    assert "remaining_debt" in row
    assert "fully_liquidated" in row
    assert row["fully_liquidated"] is False  # remaining_debt=60>0


@pytest.mark.asyncio
async def test_recent_liquidations_limit_param_bounds(client):
    """limit 必须 1-100."""
    r1 = await client.get("/api/v1/loan/recent-liquidations?limit=0")
    assert r1.status_code == 422
    r2 = await client.get("/api/v1/loan/recent-liquidations?limit=101")
    assert r2.status_code == 422


@pytest.mark.asyncio
async def test_recent_liquidations_order_desc(client):
    """排序按 triggered_at desc."""
    async with async_session_maker() as db:
        u = User(username="ord_user", casdoor_id="ord_cas",
                 cash=Decimal("0"), debt=Decimal("0"))
        db.add(u); await db.flush()
        now = datetime.now(timezone.utc)
        for offset_min, label in [(60, "old"), (0, "new")]:
            ev = LiquidationEvent(
                user_id=u.id,
                triggered_at=now - timedelta(minutes=offset_min),
                pre_cash=Decimal("0"), pre_debt=Decimal("100"),
                pre_holdings_value=Decimal("0"),
                pre_net_worth=Decimal("-100"),
                pre_margin_ratio=Decimal("-1"),
                sold_positions_count=0,
                total_proceeds=Decimal("0"),
                repaid_amount=Decimal("0"),
                remaining_debt=Decimal("100"),
                post_cash=Decimal("0"),
                trigger_source="scheduler",
            )
            db.add(ev)
        await db.commit()

    resp = await client.get("/api/v1/loan/recent-liquidations?limit=20")
    rows = [r for r in resp.json() if r["username"] == "ord_user"]
    assert len(rows) == 2
    t0 = datetime.fromisoformat(rows[0]["triggered_at"].replace("Z", "+00:00"))
    t1 = datetime.fromisoformat(rows[1]["triggered_at"].replace("Z", "+00:00"))
    assert t0 > t1
