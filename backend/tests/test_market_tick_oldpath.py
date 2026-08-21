"""老路径（writer off）→ tick 帧接线：HTTP buy 触发帧 + 双发开关。"""
import json
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import Market, MarketStatus, Outcome, User
from app.services.realtime import BROKER
from app.services.tick_broadcaster import TICK_BROADCASTER


def _parse_sse(blob: bytes) -> dict:
    text = blob.decode("utf-8")
    assert text.endswith("\n\n")
    data_line = next(l for l in text.split("\n") if l.startswith("data: "))
    return json.loads(data_line[len("data: "):])


TRADE_KEYS = {"id", "type", "outcome_id", "username", "shares", "price", "gross",
              "fee", "post_market_price", "market_prices_post", "timestamp"}


@pytest.fixture(autouse=True)
def _reset_tick():
    TICK_BROADCASTER._pending.clear()
    yield


async def _seed_user(cash: Decimal = Decimal("10000")) -> tuple[int, dict]:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        u = User(
            username=f"u_{suffix}", email=f"{suffix}@t.com",
            casdoor_id=f"cd_{suffix}",
            cash=cash, debt=Decimal("0"),
        )
        s.add(u)
        await s.flush()
        uid = u.id
        await s.commit()
    return uid, {"Authorization": f"Bearer {create_access_token(uid)}"}


async def _seed_admin() -> dict:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        u = User(
            username=f"admin_{suffix}", email=f"{suffix}@t.com",
            casdoor_id=f"cd_{suffix}",
            cash=Decimal("0"), is_superuser=True,
        )
        s.add(u)
        await s.flush()
        uid = u.id
        await s.commit()
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


async def _seed_market(n_outcomes: int = 2, b: float = 100.0) -> tuple[int, list[int]]:
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        m = Market(title=f"m_{suffix}", status=MarketStatus.TRADING, liquidity_b=b)
        s.add(m)
        await s.flush()
        oids = []
        for i in range(n_outcomes):
            o = Outcome(market_id=m.id, label=f"opt_{i}", total_shares=Decimal("0"))
            s.add(o)
            await s.flush()
            oids.append(o.id)
        await s.commit()
        return m.id, oids


@pytest_asyncio.fixture
async def seeded_market():
    mid, oids = await _seed_market()
    return mid, oids[0]


@pytest.mark.asyncio
async def test_oldpath_buy_emits_tick_and_legacy(client, seeded_market):
    market_id, outcome_id = seeded_market
    _, auth_headers = await _seed_user(cash=Decimal("1000"))
    sub, _ = await BROKER.subscribe(market_id)
    try:
        r = await client.post("/api/v1/market/buy",
                              json={"outcome_id": outcome_id, "shares": 5,
                                    "accept_any_slippage": True},
                              headers=auth_headers)
        assert r.status_code == 200, r.text
        await TICK_BROADCASTER.flush_once()
        payloads = []
        while not sub.q.empty():
            payloads.append(_parse_sse(sub.q.get_nowait()))
        legacy = [p for p in payloads if p["type"] == "trade"]
        ticks = [p for p in payloads if p["type"] == "tick"]
        assert len(legacy) == 1 and len(ticks) == 1
        assert ticks[0]["data"]["trades"][0] == legacy[0]["data"]["trade"]
        assert set(ticks[0]["data"]["trades"][0].keys()) == TRADE_KEYS   # 与 writer 路径同形状（MIN-11）
        assert ticks[0]["data"]["status"] == "trading"
        assert len(ticks[0]["data"]["prices"]) == 2
    finally:
        await BROKER.unsubscribe(market_id, sub)


@pytest.mark.asyncio
async def test_oldpath_legacy_off(client, seeded_market, monkeypatch):
    """开关关掉：老事件不再发，tick 帧照发。"""
    market_id, outcome_id = seeded_market
    _, auth_headers = await _seed_user(cash=Decimal("1000"))
    from app.services import site_config
    async def _false(session, key, default):
        if key == "legacy_trade_events":
            return False
        return default
    monkeypatch.setattr("app.api.v1.market.site_config.get_bool_or", _false)
    sub, _ = await BROKER.subscribe(market_id)
    try:
        r = await client.post("/api/v1/market/buy",
                              json={"outcome_id": outcome_id, "shares": 5,
                                    "accept_any_slippage": True},
                              headers=auth_headers)
        assert r.status_code == 200, r.text
        await TICK_BROADCASTER.flush_once()
        payloads = []
        while not sub.q.empty():
            payloads.append(_parse_sse(sub.q.get_nowait()))
        assert [p["type"] for p in payloads] == ["tick"]
    finally:
        await BROKER.unsubscribe(market_id, sub)


@pytest.mark.asyncio
async def test_oldpath_close_and_resolve_frames(client, seeded_market):
    """close → status=halt 帧；resolve → settlement 帧（老路径管理端点）。"""
    market_id, outcome_id = seeded_market
    admin_headers = await _seed_admin()
    sub, _ = await BROKER.subscribe(market_id)
    try:
        r = await client.post(f"/api/v1/market/{market_id}/close", headers=admin_headers)
        assert r.status_code == 200, r.text
        await TICK_BROADCASTER.flush_once()
        payloads = []
        while not sub.q.empty():
            payloads.append(_parse_sse(sub.q.get_nowait()))
        legacy = [p for p in payloads if p["type"] == "market_status"]
        ticks = [p for p in payloads if p["type"] == "tick"]
        assert len(legacy) == 1 and len(ticks) == 1
        assert ticks[0]["data"]["status"] == "halt"

        r = await client.post(f"/api/v1/market/{market_id}/resume", headers=admin_headers)
        assert r.status_code == 200, r.text
        await TICK_BROADCASTER.flush_once()
        while not sub.q.empty():
            sub.q.get_nowait()

        r = await client.post(f"/api/v1/market/{market_id}/resolve", headers=admin_headers,
                              json={"winning_outcome_id": outcome_id, "payout": 1})
        assert r.status_code == 200, r.text
        await TICK_BROADCASTER.flush_once()
        payloads = []
        while not sub.q.empty():
            payloads.append(_parse_sse(sub.q.get_nowait()))
        ticks = [p for p in payloads if p["type"] == "tick"]
        assert len(ticks) == 1
        settled = ticks[0]["data"]
        assert settled["status"] == "settled"
        assert settled["settlement"]["winning_outcome_id"] == outcome_id
        assert "settled_at" in settled["settlement"]
    finally:
        await BROKER.unsubscribe(market_id, sub)
