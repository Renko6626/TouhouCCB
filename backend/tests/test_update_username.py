"""用户自助改昵称接口测试。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio

from app.core.database import async_session_maker
from app.core.users import create_access_token
from app.models.base import User


async def _seed_user(username: str | None = None) -> tuple[int, dict]:
    suffix = uuid.uuid4().hex[:8]
    name = username or f"u_{suffix}"
    async with async_session_maker() as s:
        u = User(
            username=name,
            email=f"{suffix}@t.com",
            casdoor_id=f"cd_{suffix}",
            cash=Decimal("500"),
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id, {"Authorization": f"Bearer {create_access_token(u.id)}"}


@pytest.mark.asyncio
async def test_update_username_happy_path(client):
    uid, h = await _seed_user()
    new_name = f"灵梦_{uuid.uuid4().hex[:6]}"
    resp = await client.patch(
        "/api/v1/user/me/username",
        headers=h,
        json={"username": new_name},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["username"] == new_name
    assert data["changed"] is True
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.username == new_name


@pytest.mark.asyncio
async def test_update_username_same_as_current_noop(client):
    uid, h = await _seed_user(username="renko_test")
    resp = await client.patch(
        "/api/v1/user/me/username",
        headers=h,
        json={"username": "renko_test"},
    )
    assert resp.status_code == 200
    assert resp.json()["changed"] is False


@pytest.mark.asyncio
async def test_update_username_duplicate_rejected(client):
    _, h1 = await _seed_user(username="user_alpha")
    _, h2 = await _seed_user(username="user_beta")
    resp = await client.patch(
        "/api/v1/user/me/username",
        headers=h2,
        json={"username": "user_alpha"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_update_username_too_short(client):
    _, h = await _seed_user()
    resp = await client.patch(
        "/api/v1/user/me/username",
        headers=h,
        json={"username": "a"},
    )
    # pydantic min_length=2 → 422
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_username_invalid_chars(client):
    _, h = await _seed_user()
    resp = await client.patch(
        "/api/v1/user/me/username",
        headers=h,
        json={"username": "name with space"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_username_chinese_allowed(client):
    uid, h = await _seed_user()
    resp = await client.patch(
        "/api/v1/user/me/username",
        headers=h,
        json={"username": "博丽神社香客"},
    )
    assert resp.status_code == 200
    async with async_session_maker() as s:
        u = await s.get(User, uid)
        assert u.username == "博丽神社香客"


@pytest.mark.asyncio
async def test_update_username_requires_auth(client):
    resp = await client.patch(
        "/api/v1/user/me/username",
        json={"username": "anon"},
    )
    assert resp.status_code in (401, 403)
