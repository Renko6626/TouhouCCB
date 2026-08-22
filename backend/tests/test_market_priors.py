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
    assert [o.initial_shares for o in outs] == [o.total_shares for o in outs]
    assert min(q) == 0.0
    _, prices = calculate_lmsr_with_prices(q, 50.0)
    assert [round(p, 4) for p in prices] == [0.7, 0.2, 0.1]

    # 详情接口价格也一致
    r = await client.get(f"/api/v1/market/{mid}")
    assert r.status_code == 200, r.text
    got = [o["current_price"] for o in r.json()["outcomes"]]
    assert [round(p, 4) for p in got] == [0.7, 0.2, 0.1]
    assert [o["initial_shares"] for o in r.json()["outcomes"]] == q

    # 默认仍为全 0
    r = await client.post("/api/v1/market/create", headers=ah, json=base)
    assert r.status_code == 201, r.text
    async with async_session_maker() as s:
        outs = (await s.execute(select(Outcome).where(Outcome.market_id == r.json()["market_id"]))).scalars().all()
    assert all(o.total_shares == 0 and o.initial_shares == 0 for o in outs)


@pytest.mark.asyncio
async def test_backfill_snapshots_respect_seeded_q(client, setup_db):
    """带先验的市场：清掉成交时写的 market_prices_post 后，回填脚本重放必须得到同样的快照
    （脚本以前假设起手 q=0，会算错）。"""
    from sqlalchemy import text, update
    from app.models.base import Transaction
    from scripts.backfill_market_prices_post import backfill_market

    tok = await _dev_login(client, "admin_bf")
    ah = {"Authorization": f"Bearer {tok}"}
    r = await client.post("/api/v1/market/create", headers=ah, json={
        "title": "bf", "description": "", "liquidity_b": 30, "outcomes": ["A", "B", "C"],
        "tags": [], "initial_prices": [0.5, 0.3, 0.2],
    })
    assert r.status_code == 201, r.text
    mid = r.json()["market_id"]
    async with async_session_maker() as s:
        oids = list((await s.execute(
            select(Outcome.id).where(Outcome.market_id == mid).order_by(Outcome.id))).scalars().all())
    for oid, sh in [(oids[2], "5"), (oids[0], "3"), (oids[1], "2")]:
        r = await client.post("/api/v1/market/buy", headers=ah,
                              json={"outcome_id": oid, "shares": sh, "accept_any_slippage": True})
        assert r.status_code == 200, r.text
    r = await client.post("/api/v1/market/sell", headers=ah,
                          json={"outcome_id": oids[2], "shares": "2", "accept_any_slippage": True})
    assert r.status_code == 200, r.text

    async with async_session_maker() as s:
        rows = (await s.execute(
            select(Transaction.id, Transaction.market_prices_post)
            .where(Transaction.outcome_id.in_(oids)).order_by(Transaction.id))).all()
        live = {tid: snap for tid, snap in rows}
        assert all(v is not None for v in live.values())
        await s.execute(update(Transaction).where(Transaction.outcome_id.in_(oids)).values(market_prices_post=None))
        await s.commit()

    async with async_session_maker() as s:
        stats = await backfill_market(s, mid, batch=100, dry_run=False)
        assert stats["filled"] == len(live), stats
        rows = (await s.execute(
            select(Transaction.id, Transaction.market_prices_post)
            .where(Transaction.outcome_id.in_(oids)).order_by(Transaction.id))).all()
    for tid, snap in rows:
        assert snap is not None
        for a, b_ in zip(snap, live[tid]):
            assert abs(float(a) - float(b_)) < 1e-6, (tid, snap, live[tid])
