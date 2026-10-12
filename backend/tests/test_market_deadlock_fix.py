"""同市场不同 outcome 的买卖回归；经济操作由市场 writer 串行执行。"""
import uuid
from decimal import Decimal

import pytest

import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, User


async def _make_user(cash=Decimal("100000")):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(
                username=f"u_{suffix}",
                email=f"{suffix}@t.com",
                casdoor_id=f"cd_{suffix}",
                cash=cash,
                debt=Decimal("0"),
                is_superuser=False,
            )
            s.add(u)
            await s.flush()
            uid = u.id
    token = create_access_token(uid)
    return uid, {"Authorization": f"Bearer {token}"}


async def _make_market(liquidity_b: float = 10000.0, n_outcomes: int = 2):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            m = Market(
                title=f"m_{suffix}",
                liquidity_b=liquidity_b,
                status=MarketStatus.TRADING,
            )
            s.add(m)
            await s.flush()
            outcome_ids = []
            for i in range(n_outcomes):
                o = Outcome(market_id=m.id, label=f"opt_{i}", total_shares=Decimal("0"))
                s.add(o)
                await s.flush()
                outcome_ids.append(o.id)
    return m.id, outcome_ids


# ────────────────────────────────────────────────
# 功能回归：修复后 buy/sell 仍正常工作
# ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_buy_different_outcomes_same_market_both_succeed(client):
    """两个用户分别买同一市场的不同 outcome，两笔都应该 200。"""
    _, h1 = await _make_user()
    _, h2 = await _make_user()
    _, outcome_ids = await _make_market(liquidity_b=10000.0, n_outcomes=2)

    r1 = await client.post(
        "/api/v1/market/buy",
        json={"outcome_id": outcome_ids[0], "shares": 1},
        headers=h1,
    )
    r2 = await client.post(
        "/api/v1/market/buy",
        json={"outcome_id": outcome_ids[1], "shares": 1},
        headers=h2,
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text


@pytest.mark.asyncio
async def test_buy_then_sell_same_outcome_succeeds(client):
    """同一用户先买后卖同一 outcome，应正常成交。"""
    _, h = await _make_user()
    _, outcome_ids = await _make_market(liquidity_b=10000.0, n_outcomes=2)

    r = await client.post(
        "/api/v1/market/buy",
        json={"outcome_id": outcome_ids[0], "shares": 1},
        headers=h,
    )
    assert r.status_code == 200, r.text

    r = await client.post(
        "/api/v1/market/sell",
        json={"outcome_id": outcome_ids[0], "shares": 1},
        headers=h,
    )
    assert r.status_code == 200, r.text
