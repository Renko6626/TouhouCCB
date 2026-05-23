import pytest, uuid
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.models.title import Title, UserTitle


async def _mk_user_with_title():
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}")
            t = Title(name=f"VIP_{suffix}")
            s.add(u); s.add(t); await s.flush()
            s.add(UserTitle(user_id=u.id, title_id=t.id, source="admin"))
            return u.id, t.id, {"Authorization": f"Bearer {create_access_token(u.id)}"}


@pytest.mark.asyncio
async def test_equip_owned_title(client):
    uid, tid, h = await _mk_user_with_title()
    r = await client.post("/api/v1/title/me/equip",
                          json={"title_id": tid}, headers=h)
    assert r.status_code == 200
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.equipped_title_id == tid


@pytest.mark.asyncio
async def test_equip_null_unequips(client):
    uid, tid, h = await _mk_user_with_title()
    await client.post("/api/v1/title/me/equip",
                      json={"title_id": tid}, headers=h)
    r = await client.post("/api/v1/title/me/equip",
                          json={"title_id": None}, headers=h)
    assert r.status_code == 200
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.equipped_title_id is None


@pytest.mark.asyncio
async def test_equip_not_owned_title_403(client):
    uid, tid, h = await _mk_user_with_title()
    async with async_session_maker() as s:
        async with s.begin():
            t2 = Title(name=f"OTHER_{uuid.uuid4().hex[:6]}")
            s.add(t2); await s.flush()
            other_tid = t2.id
    r = await client.post("/api/v1/title/me/equip",
                          json={"title_id": other_tid}, headers=h)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_equip_nonexistent_title_403_or_404(client):
    uid, tid, h = await _mk_user_with_title()
    r = await client.post("/api/v1/title/me/equip",
                          json={"title_id": 999999}, headers=h)
    assert r.status_code in (403, 404)
