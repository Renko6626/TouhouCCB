"""建市场时给先验 initial_prices：q_i = b·(ln p_i − ln p_min)，初始价精确等于先验。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import math
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import Outcome
from app.services.lmsr import calculate_lmsr_with_prices, seed_shares_from_prices


def test_seed_shares_reproduce_prices():
    prices = [0.6, 0.3, 0.1]
    q = seed_shares_from_prices(prices, 100.0)
    assert min(q) == Decimal("0")
    _, got = calculate_lmsr_with_prices([float(x) for x in q], 100.0)
    for g, p in zip(got, prices):
        assert abs(g - p) < 1e-6


def test_seed_shares_uniform_is_zero():
    assert seed_shares_from_prices([0.5, 0.5], 100.0) == [Decimal("0"), Decimal("0")]


async def _dev_login(client, name):
    r = await client.post("/api/v1/auth/dev-login", json={"username": name})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_create_market_with_priors(client, setup_db):
    tok = await _dev_login(client, "admin_p")
    ah = {"Authorization": f"Bearer {tok}"}
    base = {"title": "m", "description": "", "liquidity_b": 50, "outcomes": ["A", "B", "C"], "tags": []}

    # 长度不符 / 含 0 / 和不为 1 → 422
    for bad in ([0.5, 0.5], [0.0, 0.5, 0.5], [0.5, 0.5, 0.5]):
        r = await client.post("/api/v1/market/create", headers=ah, json={**base, "initial_prices": bad})
        assert r.status_code == 422, (bad, r.text)

    r = await client.post("/api/v1/market/create", headers=ah, json={**base, "initial_prices": [0.7, 0.2, 0.1]})
    assert r.status_code == 201, r.text
    mid = r.json()["market_id"]

    async with async_session_maker() as s:
        outs = (await s.execute(select(Outcome).where(Outcome.market_id == mid).order_by(Outcome.id))).scalars().all()
    q = [float(o.total_shares) for o in outs]
    assert min(q) == 0.0
    _, prices = calculate_lmsr_with_prices(q, 50.0)
    assert [round(p, 4) for p in prices] == [0.7, 0.2, 0.1]

    # 详情接口价格也一致
    r = await client.get(f"/api/v1/market/{mid}")
    assert r.status_code == 200, r.text
    got = [o["current_price"] for o in r.json()["outcomes"]]
    assert [round(p, 4) for p in got] == [0.7, 0.2, 0.1]

    # 默认仍为全 0
    r = await client.post("/api/v1/market/create", headers=ah, json=base)
    assert r.status_code == 201, r.text
    async with async_session_maker() as s:
        outs = (await s.execute(select(Outcome).where(Outcome.market_id == r.json()["market_id"]))).scalars().all()
    assert all(o.total_shares == 0 for o in outs)
