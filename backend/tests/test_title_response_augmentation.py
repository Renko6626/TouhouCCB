import pytest, uuid
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User, Market, Outcome
from app.models.title import Title, UserTitle, MarketRequiredTitle


async def _mk():
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"u{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}")
            t = Title(name=f"VIP_{suffix}", color="#FFD700", icon="★")
            s.add(u); s.add(t); await s.flush()
            s.add(UserTitle(user_id=u.id, title_id=t.id, source="admin"))
            return u.id, t.id, {"Authorization": f"Bearer {create_access_token(u.id)}"}


@pytest.mark.asyncio
async def test_user_summary_includes_titles(client):
    uid, tid, h = await _mk()
    r = await client.get("/api/v1/user/summary", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert "equipped_title" in body
    assert "all_titles" in body
    assert body["equipped_title"] is None
    assert len(body["all_titles"]) == 1
    assert body["all_titles"][0]["name"].startswith("VIP_")


@pytest.mark.asyncio
async def test_user_summary_equipped_chip(client):
    uid, tid, h = await _mk()
    async with async_session_maker() as s:
        async with s.begin():
            u = await s.get(User, uid)
            u.equipped_title_id = tid
            s.add(u)
    r = await client.get("/api/v1/user/summary", headers=h)
    body = r.json()
    assert body["equipped_title"] is not None
    assert body["equipped_title"]["color"] == "#FFD700"


@pytest.mark.asyncio
async def test_market_detail_required_titles(client):
    uid, tid, h = await _mk()
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            m = Market(title=f"M_{suffix}", liquidity_b=100.0)
            s.add(m); await s.flush()
            o = Outcome(market_id=m.id, label="Yes")
            s.add(o)
            s.add(MarketRequiredTitle(market_id=m.id, title_id=tid))
            await s.flush()
            mid = m.id
    r = await client.get(f"/api/v1/market/{mid}", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "required_titles" in body
    assert "user_can_trade" in body
    assert body["user_can_trade"] is True
    assert len(body["required_titles"]) == 1


@pytest.mark.asyncio
async def test_market_detail_user_cannot_trade(client):
    """A user without the required title sees user_can_trade=false."""
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"u{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}")
            t = Title(name=f"VIP_{suffix}", color="#FFD700", icon="★")
            m = Market(title=f"M_{suffix}", liquidity_b=100.0)
            s.add(u); s.add(t); s.add(m); await s.flush()
            o = Outcome(market_id=m.id, label="Yes")
            s.add(o)
            s.add(MarketRequiredTitle(market_id=m.id, title_id=t.id))
            await s.flush()
            mid, uid = m.id, u.id
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    r = await client.get(f"/api/v1/market/{mid}", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["user_can_trade"] is False
    assert len(body["required_titles"]) == 1


@pytest.mark.asyncio
async def test_market_list_has_required_titles(client):
    uid, tid, h = await _mk()
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            m = Market(title=f"GatedM_{suffix}", liquidity_b=100.0)
            s.add(m); await s.flush()
            o = Outcome(market_id=m.id, label="Yes")
            s.add(o)
            s.add(MarketRequiredTitle(market_id=m.id, title_id=tid))
    r = await client.get("/api/v1/market/list", headers=h)
    assert r.status_code == 200
    found = next((item for item in r.json() if item.get("title", "").startswith(f"GatedM_{suffix}")), None)
    assert found is not None
    assert "required_titles" in found
    assert len(found["required_titles"]) == 1
