import pytest, uuid
from decimal import Decimal
from sqlalchemy import delete
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User, Market, Outcome
from app.models.title import Title, UserTitle, MarketRequiredTitle


async def _mk_market_with_title_gate(required_titles_names: tuple = ()):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            m = Market(title=f"M_{suffix}", liquidity_b=100.0)
            s.add(m); await s.flush()
            o = Outcome(market_id=m.id, label="Yes")
            o2 = Outcome(market_id=m.id, label="No")
            s.add(o); s.add(o2); await s.flush()
            tids = []
            for name in required_titles_names:
                t = Title(name=f"{name}_{suffix}")
                s.add(t); await s.flush()
                s.add(MarketRequiredTitle(market_id=m.id, title_id=t.id))
                tids.append(t.id)
            return m.id, o.id, tids


async def _mk_user_with_titles(*title_ids, cash=Decimal("1000")):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"u{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}", cash=cash)
            s.add(u); await s.flush()
            for tid in title_ids:
                s.add(UserTitle(user_id=u.id, title_id=tid, source="admin"))
            return u.id, {"Authorization": f"Bearer {create_access_token(u.id)}"}


@pytest.mark.asyncio
async def test_no_gate_anyone_buys(client):
    mid, oid, _ = await _mk_market_with_title_gate(required_titles_names=())
    uid, h = await _mk_user_with_titles()
    r = await client.post("/api/v1/market/buy",
                          json={"outcome_id": oid, "shares": "1"}, headers=h)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_gate_blocks_user_without_required_title(client):
    mid, oid, tids = await _mk_market_with_title_gate(required_titles_names=("VIP",))
    uid, h = await _mk_user_with_titles()
    r = await client.post("/api/v1/market/buy",
                          json={"outcome_id": oid, "shares": "1"}, headers=h)
    assert r.status_code == 403
    body = r.json()
    assert body.get("detail") == "MARKET_TITLE_REQUIRED"


@pytest.mark.asyncio
async def test_gate_any_of_passes(client):
    mid, oid, tids = await _mk_market_with_title_gate(required_titles_names=("VIP", "Beta"))
    uid, h = await _mk_user_with_titles(tids[0])
    r = await client.post("/api/v1/market/buy",
                          json={"outcome_id": oid, "shares": "1"}, headers=h)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_sell_not_gated(client):
    """卖出 / 结算不受 gate 约束 — 防止 title 撤销导致用户持仓被锁。"""
    mid, oid, tids = await _mk_market_with_title_gate(required_titles_names=("VIP",))
    uid, h = await _mk_user_with_titles(tids[0], cash=Decimal("1000"))
    await client.post("/api/v1/market/buy",
                      json={"outcome_id": oid, "shares": "1"}, headers=h)
    async with async_session_maker() as s:
        async with s.begin():
            await s.execute(delete(UserTitle).where(
                UserTitle.user_id == uid, UserTitle.title_id == tids[0],
            ))
    r = await client.post("/api/v1/market/sell",
                          json={"outcome_id": oid, "shares": "1"}, headers=h)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_quote_not_gated(client):
    """quote 是只读，不 gate。"""
    mid, oid, tids = await _mk_market_with_title_gate(required_titles_names=("VIP",))
    uid, h = await _mk_user_with_titles()
    r = await client.post("/api/v1/market/quote",
                          json={"outcome_id": oid, "shares": "1", "side": "buy"}, headers=h)
    assert r.status_code == 200, r.text
