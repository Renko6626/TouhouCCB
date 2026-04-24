# 认证接口测试
# 注意：Casdoor 回调 (/auth/callback) 需要真实的 Casdoor 服务，
# 这里只测试不依赖 Casdoor 的端点。

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
import uuid
from decimal import Decimal
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.core.database import engine, async_session_maker
from app.core.users import create_access_token, create_refresh_token
from app.models.base import User
from sqlmodel import SQLModel
import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


@pytest_asyncio.fixture
async def client():
    from asgi_lifespan import LifespanManager
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:8004") as ac:
            yield ac


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
