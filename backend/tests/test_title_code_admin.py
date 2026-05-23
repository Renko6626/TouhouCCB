import pytest, uuid
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.models.title import Title


async def _mk_user(superuser=False):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}", is_superuser=superuser)
            s.add(u); await s.flush()
            return u.id, {"Authorization": f"Bearer {create_access_token(u.id)}"}


async def _mk_title(name="VIP"):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            t = Title(name=f"{name}_{suffix}")
            s.add(t); await s.flush()
            return t.id


@pytest.mark.asyncio
async def test_create_batch(client):
    _, h = await _mk_user(superuser=True)
    tid = await _mk_title()
    r = await client.post("/api/v1/admin/title-batches",
                          json={"title_id": tid, "name": "2026-Q2", "description": ""},
                          headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title_id"] == tid
    assert body["name"] == "2026-Q2"
    assert body["total"] == 0
    assert body["used"] == 0


@pytest.mark.asyncio
async def test_create_batch_inactive_title_rejected(client):
    _, h = await _mk_user(superuser=True)
    tid = await _mk_title()
    await client.patch(f"/api/v1/admin/titles/{tid}",
                       json={"is_active": False}, headers=h)
    r = await client.post("/api/v1/admin/title-batches",
                          json={"title_id": tid, "name": "X"}, headers=h)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_list_batches_returns_counts(client):
    _, h = await _mk_user(superuser=True)
    tid = await _mk_title()
    await client.post("/api/v1/admin/title-batches",
                      json={"title_id": tid, "name": "B1"}, headers=h)
    r = await client.get("/api/v1/admin/title-batches", headers=h)
    assert r.status_code == 200
    arr = r.json()
    assert len(arr) >= 1
    assert any(b["name"] == "B1" for b in arr)


@pytest.mark.asyncio
async def test_create_batch_requires_superuser(client):
    _, h = await _mk_user(superuser=False)
    r = await client.post("/api/v1/admin/title-batches",
                          json={"title_id": 1, "name": "X"}, headers=h)
    assert r.status_code in (401, 403)
