import pytest, uuid
from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User
from app.models.title import Title, UserTitle


async def _mk_user(superuser=False):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            u = User(username=f"u_{suffix}", email=f"{suffix}@t.com",
                    casdoor_id=f"cd_{suffix}", is_superuser=superuser)
            s.add(u); await s.flush()
            return u.id, {"Authorization": f"Bearer {create_access_token(u.id)}"}


async def _mk_title(name="VIP", color="#FFD700", sort=10, is_active=True):
    suffix = uuid.uuid4().hex[:6]
    async with async_session_maker() as s:
        async with s.begin():
            t = Title(name=f"{name}_{suffix}", color=color,
                      sort_order=sort, is_active=is_active)
            s.add(t); await s.flush()
            return t.id


async def _grant(uid, tid, source="admin"):
    async with async_session_maker() as s:
        async with s.begin():
            s.add(UserTitle(user_id=uid, title_id=tid, source=source))


@pytest.mark.asyncio
async def test_my_titles_empty(client):
    uid, h = await _mk_user()
    r = await client.get("/api/v1/title/me", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["equipped_title_id"] is None
    assert body["titles"] == []


@pytest.mark.asyncio
async def test_my_titles_lists_owned_sorted(client):
    uid, h = await _mk_user()
    tid_a = await _mk_title(name="A", sort=30)
    tid_b = await _mk_title(name="B", sort=10)
    await _grant(uid, tid_a)
    await _grant(uid, tid_b)
    r = await client.get("/api/v1/title/me", headers=h)
    assert r.status_code == 200
    titles = r.json()["titles"]
    # sort_order asc — B(10) before A(30)
    assert titles[0]["title"]["name"].startswith("B_")
    assert titles[1]["title"]["name"].startswith("A_")


@pytest.mark.asyncio
async def test_catalog_public_excludes_inactive(client):
    _, h = await _mk_user()
    active_id = await _mk_title(name="Active", is_active=True)
    hidden_id = await _mk_title(name="Hidden", is_active=False)
    r = await client.get("/api/v1/title/catalog", headers=h)
    assert r.status_code == 200
    ids = [t["id"] for t in r.json()]
    assert active_id in ids
    assert hidden_id not in ids


@pytest.mark.asyncio
async def test_users_equipped_returns_chip(client):
    uid, _ = await _mk_user()
    tid = await _mk_title(name="VIP")
    await _grant(uid, tid)
    async with async_session_maker() as s:
        async with s.begin():
            u = await s.get(User, uid)
            u.equipped_title_id = tid
            s.add(u)
    _, h = await _mk_user()
    r = await client.get(f"/api/v1/title/users/{uid}/equipped", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body is not None
    assert body["name"].startswith("VIP_")


@pytest.mark.asyncio
async def test_users_equipped_null_when_not_set(client):
    uid, _ = await _mk_user()
    _, h = await _mk_user()
    r = await client.get(f"/api/v1/title/users/{uid}/equipped", headers=h)
    assert r.status_code == 200
    assert r.json() is None
