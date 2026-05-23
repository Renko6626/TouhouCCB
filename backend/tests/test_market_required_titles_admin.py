import pytest, uuid
from sqlalchemy import select, func
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User, Market
from app.models.title import Title, MarketRequiredTitle


async def _mk():
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            admin = User(username=f"a_{suffix}", email=f"a{suffix}@t.com",
                          casdoor_id=f"acd_{suffix}", is_superuser=True)
            m = Market(title=f"M_{suffix}", liquidity_b=100.0)
            t1 = Title(name=f"VIP_{suffix}")
            t2 = Title(name=f"Beta_{suffix}")
            s.add(admin); s.add(m); s.add(t1); s.add(t2); await s.flush()
            return admin.id, m.id, t1.id, t2.id, {"Authorization": f"Bearer {create_access_token(admin.id)}"}


@pytest.mark.asyncio
async def test_get_required_titles_empty(client):
    _, mid, _, _, h = await _mk()
    r = await client.get(f"/api/v1/admin/markets/{mid}/required-titles", headers=h)
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_put_replaces_required_titles(client):
    _, mid, t1, t2, h = await _mk()
    r = await client.put(f"/api/v1/admin/markets/{mid}/required-titles",
                          json={"title_ids": [t1, t2]}, headers=h)
    assert r.status_code == 200
    async with async_session_maker() as s:
        cnt = (await s.execute(
            select(func.count()).select_from(MarketRequiredTitle).where(
                MarketRequiredTitle.market_id == mid,
            )
        )).scalar_one()
        assert cnt == 2


@pytest.mark.asyncio
async def test_put_overwrites_existing(client):
    _, mid, t1, t2, h = await _mk()
    await client.put(f"/api/v1/admin/markets/{mid}/required-titles",
                     json={"title_ids": [t1]}, headers=h)
    await client.put(f"/api/v1/admin/markets/{mid}/required-titles",
                     json={"title_ids": [t2]}, headers=h)
    async with async_session_maker() as s:
        rows = list((await s.execute(
            select(MarketRequiredTitle.title_id).where(
                MarketRequiredTitle.market_id == mid,
            )
        )).scalars().all())
        assert rows == [t2]


@pytest.mark.asyncio
async def test_put_empty_clears(client):
    _, mid, t1, _, h = await _mk()
    await client.put(f"/api/v1/admin/markets/{mid}/required-titles",
                     json={"title_ids": [t1]}, headers=h)
    await client.put(f"/api/v1/admin/markets/{mid}/required-titles",
                     json={"title_ids": []}, headers=h)
    async with async_session_maker() as s:
        cnt = (await s.execute(
            select(func.count()).select_from(MarketRequiredTitle).where(
                MarketRequiredTitle.market_id == mid,
            )
        )).scalar_one()
        assert cnt == 0


@pytest.mark.asyncio
async def test_put_market_not_found(client):
    _, _, t1, _, h = await _mk()
    r = await client.put(f"/api/v1/admin/markets/999999/required-titles",
                          json={"title_ids": [t1]}, headers=h)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_put_invalid_title_id_rejected(client):
    _, mid, t1, _, h = await _mk()
    r = await client.put(f"/api/v1/admin/markets/{mid}/required-titles",
                          json={"title_ids": [t1, 999999]}, headers=h)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_put_dedup_duplicates(client):
    _, mid, t1, _, h = await _mk()
    r = await client.put(f"/api/v1/admin/markets/{mid}/required-titles",
                          json={"title_ids": [t1, t1, t1]}, headers=h)
    assert r.status_code == 200
    async with async_session_maker() as s:
        cnt = (await s.execute(
            select(func.count()).select_from(MarketRequiredTitle).where(
                MarketRequiredTitle.market_id == mid,
            )
        )).scalar_one()
        assert cnt == 1


@pytest.mark.asyncio
async def test_requires_superuser(client):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"nu_{suffix}", email=f"nu{suffix}@t.com",
                    casdoor_id=f"nucd_{suffix}")
            s.add(u); await s.flush()
            uid = u.id
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    r = await client.get("/api/v1/admin/markets/1/required-titles", headers=h)
    assert r.status_code in (401, 403)
