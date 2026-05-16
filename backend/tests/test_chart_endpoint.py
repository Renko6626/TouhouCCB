"""/chart/candles endpoint 端到端测试。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlmodel import select

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, OutcomeCandle, User


async def _seed_user(cash=Decimal("10000")) -> dict:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                 casdoor_id=f"cd_{suffix}", cash=cash)
        s.add(u)
        await s.flush()
        uid = u.id
        await s.commit()
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


async def _seed_market(b=100.0) -> tuple[int, list[int]]:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(title=f"m_{suffix}", status=MarketStatus.TRADING, liquidity_b=b)
        s.add(m)
        await s.flush()
        oids = []
        for i in range(2):
            o = Outcome(market_id=m.id, label=f"opt_{i}", total_shares=Decimal("0"))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        await s.commit()
        return m.id, oids


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.mark.asyncio
async def test_candles_direct_read_10s(client):
    mid, oids = await _seed_market()
    headers = await _seed_user()
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    now = datetime.now(timezone.utc)
    resp = await client.get("/api/v1/chart/candles", params={
        "outcome_id": oids[0],
        "interval": "10s",
        "from_ts": _iso(now - timedelta(minutes=10)),
        "to_ts": _iso(now + timedelta(seconds=10)),
        "fill": False,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["candles"]) >= 1
    c0 = data["candles"][0]
    assert "o" in c0 and "h" in c0 and "l" in c0 and "c" in c0
    assert c0["n"] >= 1


@pytest.mark.asyncio
async def test_candles_rollup_30s(client):
    """30s rollup：interval=30s 走 INTERVAL_ROUTE→('10s', 3)。"""
    mid, oids = await _seed_market()
    headers = await _seed_user()
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    now = datetime.now(timezone.utc)
    resp = await client.get("/api/v1/chart/candles", params={
        "outcome_id": oids[0],
        "interval": "30s",
        "from_ts": _iso(now - timedelta(minutes=10)),
        "to_ts": _iso(now + timedelta(seconds=10)),
        "fill": False,
    })
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["candles"]) >= 1


@pytest.mark.asyncio
async def test_candles_rollup_5m(client):
    mid, oids = await _seed_market()
    headers = await _seed_user()
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    now = datetime.now(timezone.utc)
    resp = await client.get("/api/v1/chart/candles", params={
        "outcome_id": oids[0],
        "interval": "5m",
        "from_ts": _iso(now - timedelta(hours=1)),
        "to_ts": _iso(now + timedelta(minutes=5)),
        "fill": False,
    })
    assert resp.status_code == 200
    assert len(resp.json()["candles"]) >= 1


@pytest.mark.asyncio
async def test_candles_fill_true_pads_empty_buckets(client):
    """fill=true 时空桶用 prev_close 填。"""
    mid, oids = await _seed_market()
    headers = await _seed_user()
    await client.post("/api/v1/market/buy", headers=headers,
                      json={"outcome_id": oids[0], "shares": 1})

    now = datetime.now(timezone.utc)
    resp = await client.get("/api/v1/chart/candles", params={
        "outcome_id": oids[0],
        "interval": "10s",
        "from_ts": _iso(now - timedelta(minutes=2)),
        "to_ts": _iso(now + timedelta(seconds=10)),
        "fill": True,
    })
    assert resp.status_code == 200
    candles = resp.json()["candles"]
    assert len(candles) >= 12
    empty_buckets = [c for c in candles if c["n"] == 0]
    for c in empty_buckets:
        assert c["o"] == c["h"] == c["l"] == c["c"]


@pytest.mark.asyncio
async def test_candles_unsupported_interval_400(client):
    mid, oids = await _seed_market()
    now = datetime.now(timezone.utc)
    resp = await client.get("/api/v1/chart/candles", params={
        "outcome_id": oids[0],
        "interval": "2h",
        "from_ts": _iso(now - timedelta(hours=1)),
        "to_ts": _iso(now),
    })
    assert resp.status_code in (400, 422)


@pytest.mark.asyncio
async def test_chart_price_endpoint_removed(client):
    """/chart/price 应该不再存在。"""
    now = datetime.now(timezone.utc)
    resp = await client.get("/api/v1/chart/price", params={
        "outcome_id": 1,
        "from_ts": _iso(now - timedelta(hours=1)),
        "to_ts": _iso(now),
    })
    assert resp.status_code == 404
