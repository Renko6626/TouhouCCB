"""阶段 3 /user/summary 与 /user/holdings 新契约（spec §6.4）。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, Position, SiteConfig, User


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


async def _seed_position(uid, shares="10", cost="5.5", total_shares="10"):
    async with async_session_maker() as s:
        m = Market(title="t", description="", liquidity_b=100.0,
                   status=MarketStatus.TRADING)
        s.add(m)
        await s.flush()
        o = Outcome(market_id=m.id, label="a", total_shares=Decimal(total_shares))
        o2 = Outcome(market_id=m.id, label="b", total_shares=Decimal("0"))
        s.add(o); s.add(o2)
        await s.flush()
        s.add(Position(user_id=uid, outcome_id=o.id,
                       amount=Decimal(shares), cost_basis=Decimal(cost)))
        await s.commit()
        return m.id, o.id


REMOVED_FIELDS = [
    "holdings_value", "holdings_value_liquidation", "net_worth",
    "net_worth_liquidation", "unrealized_pnl", "unrealized_pnl_liquidation",
    "total_cost_basis", "rank", "margin_ratio",
]


@pytest.mark.asyncio
async def test_summary_new_contract_shape(client):
    uid, h = await _make_user(cash=Decimal("1000.123456"))
    mid, oid = await _seed_position(uid, shares="10.5", cost="5.123456")
    r = await client.get("/api/v1/user/summary", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()

    for f in REMOVED_FIELDS:
        assert f not in body, f"已删字段泄漏: {f}"

    assert body["cash"] == 1000.123456          # 6dp 全精度，非 2dp
    assert body["debt"] == 0.0
    assert body["positions"] == [{
        "outcome_id": oid, "market_id": mid,
        "amount": 10.5, "cost_basis": 5.123456,
    }]
    assert body["margin_status"] == "healthy"
    assert body["liquidation_protected"] is False
    assert isinstance(body["sell_fee_rate"], (int, float))
    # rank_thresholds：6 条、降序、末条 null 兜底
    rt = body["rank_thresholds"]
    assert [x["title"] for x in rt] == \
        ["ZUN", "炒炒币大亨", "妖怪操盘手", "天狗交易员", "人里居民", "人类灵(已爆仓)"]
    assert rt[0]["min_net_worth"] == 30000.0
    assert rt[-1]["min_net_worth"] is None
    assert "equipped_title" in body and "all_titles" in body


@pytest.mark.asyncio
async def test_summary_margin_status_still_server_side(client):
    """debt>0 才算 LCV：无持仓 cash=400 debt=1000 → ratio=-0.6 → danger。"""
    await _seed_liquidation_config(hard="0.2", soft="0.5")
    _, h = await _make_user(cash=Decimal("400"), debt=Decimal("1000"))
    r = await client.get("/api/v1/user/summary", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["margin_status"] == "danger"


@pytest.mark.asyncio
async def test_holdings_slim_contract(client):
    uid, h = await _make_user()
    mid, oid = await _seed_position(uid, shares="10.5", cost="5.123456")
    r = await client.get("/api/v1/user/holdings", headers=h)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row == {
        "market_id": mid, "market_title": "t",
        "outcome_id": oid, "outcome_label": "a",
        "amount": 10.5, "cost_basis": 5.123456,
    }
