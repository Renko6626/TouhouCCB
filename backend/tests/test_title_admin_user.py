import pytest, uuid
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.models.title import Title, UserTitle


async def _mk():
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"u{suffix}@t.com",
                    casdoor_id=f"ucd_{suffix}")
            admin = User(username=f"a_{suffix}", email=f"a{suffix}@t.com",
                          casdoor_id=f"acd_{suffix}", is_superuser=True)
            t = Title(name=f"VIP_{suffix}")
            s.add(u); s.add(admin); s.add(t); await s.flush()
            return u.id, t.id, {"Authorization": f"Bearer {create_access_token(admin.id)}"}


@pytest.mark.asyncio
async def test_admin_grant_title(client):
    uid, tid, h = await _mk()
    r = await client.post(f"/api/v1/admin/users/{uid}/titles",
                          json={"title_id": tid}, headers=h)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_admin_grant_idempotent(client):
    uid, tid, h = await _mk()
    await client.post(f"/api/v1/admin/users/{uid}/titles",
                      json={"title_id": tid}, headers=h)
    r = await client.post(f"/api/v1/admin/users/{uid}/titles",
                          json={"title_id": tid}, headers=h)
    assert r.status_code == 200  # 幂等


@pytest.mark.asyncio
async def test_admin_revoke_title(client):
    uid, tid, h = await _mk()
    await client.post(f"/api/v1/admin/users/{uid}/titles",
                      json={"title_id": tid}, headers=h)
    r = await client.delete(f"/api/v1/admin/users/{uid}/titles/{tid}", headers=h)
    assert r.status_code == 200
    async with async_session_maker() as s:
        from sqlalchemy import select
        ut = (await s.execute(select(UserTitle).where(
            UserTitle.user_id == uid, UserTitle.title_id == tid,
        ))).scalar_one_or_none()
        assert ut is None


@pytest.mark.asyncio
async def test_admin_revoke_clears_equipped(client):
    uid, tid, h = await _mk()
    await client.post(f"/api/v1/admin/users/{uid}/titles",
                      json={"title_id": tid}, headers=h)
    async with async_session_maker() as s:
        async with s.begin():
            u = await s.get(User, uid)
            u.equipped_title_id = tid
            s.add(u)
    await client.delete(f"/api/v1/admin/users/{uid}/titles/{tid}", headers=h)
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.equipped_title_id is None


@pytest.mark.asyncio
async def test_admin_revoke_not_owned_404(client):
    uid, tid, h = await _mk()
    r = await client.delete(f"/api/v1/admin/users/{uid}/titles/{tid}", headers=h)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_admin_grant_inactive_title_rejected(client):
    uid, tid, h = await _mk()
    await client.patch(f"/api/v1/admin/titles/{tid}",
                       json={"is_active": False}, headers=h)
    r = await client.post(f"/api/v1/admin/users/{uid}/titles",
                          json={"title_id": tid}, headers=h)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_admin_user_summary(client):
    uid, _, h = await _mk()
    r = await client.get(f"/api/v1/admin/users/{uid}/summary", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert "cash" in body
    assert "debt" in body


@pytest.mark.asyncio
async def test_admin_user_summary_404(client):
    uid, _, h = await _mk()
    r = await client.get("/api/v1/admin/users/999999/summary", headers=h)
    assert r.status_code == 404
