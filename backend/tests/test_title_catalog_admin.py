import pytest, uuid
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.models.title import Title


async def _make_user(superuser=False):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}", is_superuser=superuser)
            s.add(u); await s.flush()
            uid = u.id
    return uid, {"Authorization": f"Bearer {create_access_token(uid)}"}


@pytest.mark.asyncio
async def test_admin_create_title_minimum(client):
    _, h = await _make_user(superuser=True)
    r = await client.post("/api/v1/admin/titles",
                          json={"name": "VIP"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "VIP"
    assert body["is_active"] is True
    assert body["color"] == "#000000"


@pytest.mark.asyncio
async def test_admin_create_title_full(client):
    _, h = await _make_user(superuser=True)
    r = await client.post("/api/v1/admin/titles",
                          json={"name":"Beta","description":"内测","color":"#FF0000",
                                "icon":"β","sort_order":50}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["color"] == "#FF0000"


@pytest.mark.asyncio
async def test_admin_create_title_duplicate_name(client):
    _, h = await _make_user(superuser=True)
    await client.post("/api/v1/admin/titles", json={"name":"VIP"}, headers=h)
    r = await client.post("/api/v1/admin/titles", json={"name":"VIP"}, headers=h)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_admin_titles_list(client):
    _, h = await _make_user(superuser=True)
    await client.post("/api/v1/admin/titles", json={"name":"A","sort_order":20}, headers=h)
    await client.post("/api/v1/admin/titles", json={"name":"B","sort_order":10}, headers=h)
    r = await client.get("/api/v1/admin/titles", headers=h)
    assert r.status_code == 200
    names = [t["name"] for t in r.json()]
    assert names == ["B", "A"]  # 按 sort_order asc


@pytest.mark.asyncio
async def test_admin_patch_title(client):
    _, h = await _make_user(superuser=True)
    r = await client.post("/api/v1/admin/titles", json={"name":"VIP"}, headers=h)
    tid = r.json()["id"]
    r = await client.patch(f"/api/v1/admin/titles/{tid}",
                            json={"color":"#0000FF","is_active":False}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["color"] == "#0000FF"
    assert body["is_active"] is False


@pytest.mark.asyncio
async def test_admin_titles_requires_superuser(client):
    _, h = await _make_user(superuser=False)
    r = await client.post("/api/v1/admin/titles", json={"name":"VIP"}, headers=h)
    assert r.status_code in (401, 403)
