# 认证接口测试
# 注意：Casdoor 回调 (/auth/callback) 需要真实的 Casdoor 服务，
# 这里只测试不依赖 Casdoor 的端点。

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
import uuid
from decimal import Decimal
from app.core.database import async_session_maker
from app.core.users import create_access_token, create_refresh_token
from app.models.base import User
import pytest_asyncio


@pytest_asyncio.fixture
async def test_user():
    suffix = uuid.uuid4().hex[:4]
    async with async_session_maker() as session:
        async with session.begin():
            user = User(
                username=f"test_{suffix}",
                email=f"test_{suffix}@test.com",
                casdoor_id=f"casdoor_{suffix}",
                cash=Decimal("100"),
                debt=Decimal("0"),
            )
            session.add(user)
            await session.flush()
            uid = user.id
    return uid


# --- 1. /me 接口测试 ---
@pytest.mark.asyncio
async def test_get_me_with_valid_token(client, test_user):
    token = create_access_token(test_user)
    headers = {"Authorization": f"Bearer {token}"}
    res = await client.get("/api/v1/auth/me", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == test_user
    assert data["cash"] == 100.0


@pytest.mark.asyncio
async def test_get_me_without_token(client):
    res = await client.get("/api/v1/auth/me")
    # HTTPBearer 在缺 token 时返回的状态码随 FastAPI 版本微调（可能 401 或 403），
    # 只要是 4xx 且 >= 401（未授权系）即可
    assert res.status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_me_with_invalid_token(client):
    headers = {"Authorization": "Bearer invalid_token_here"}
    res = await client.get("/api/v1/auth/me", headers=headers)
    assert res.status_code == 401


# --- 2. Refresh Token 测试 ---
@pytest.mark.asyncio
async def test_refresh_token_success(client, test_user):
    refresh = create_refresh_token(test_user)
    res = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data


@pytest.mark.asyncio
async def test_refresh_with_access_token_fails(client, test_user):
    """access token 不能当作 refresh token 使用"""
    access = create_access_token(test_user)
    res = await client.post("/api/v1/auth/refresh", json={"refresh_token": access})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_refresh_with_invalid_token_fails(client):
    res = await client.post("/api/v1/auth/refresh", json={"refresh_token": "garbage"})
    assert res.status_code == 401


# --- 3. TOS accept-tos 接口测试 ---
@pytest.mark.asyncio
async def test_accept_tos_first_time_writes_timestamp(client, test_user):
    """新用户 tos_accepted_at 默认 null；调用 accept-tos 后变成非 null"""
    token = create_access_token(test_user)
    headers = {"Authorization": f"Bearer {token}"}

    me_before = await client.get("/api/v1/auth/me", headers=headers)
    assert me_before.json()["tos_accepted_at"] is None

    res = await client.post("/api/v1/auth/accept-tos", headers=headers)
    assert res.status_code == 200
    assert res.json()["tos_accepted_at"] is not None

    me_after = await client.get("/api/v1/auth/me", headers=headers)
    assert me_after.json()["tos_accepted_at"] is not None


@pytest.mark.asyncio
async def test_accept_tos_idempotent(client, test_user):
    """连续调两次 accept-tos，第二次不改写时间戳"""
    token = create_access_token(test_user)
    headers = {"Authorization": f"Bearer {token}"}

    first = await client.post("/api/v1/auth/accept-tos", headers=headers)
    first_ts = first.json()["tos_accepted_at"]
    assert first_ts is not None

    second = await client.post("/api/v1/auth/accept-tos", headers=headers)
    assert second.status_code == 200
    assert second.json()["tos_accepted_at"] == first_ts


@pytest.mark.asyncio
async def test_accept_tos_requires_auth(client):
    """未登录调用 accept-tos 返回 4xx 未授权"""
    res = await client.post("/api/v1/auth/accept-tos")
    assert res.status_code in (401, 403)
