"""dev-only mock 登录 /auth/dev-login。

让本地贡献者不配 Casdoor 也能登录调试。**生产必须 404**（防 auth bypass footgun）。
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from decimal import Decimal

import pytest
from sqlalchemy import select, func

from app.core.config import settings
from app.core.database import async_session_maker
from app.models.base import User


@pytest.mark.asyncio
async def test_dev_login_creates_user_and_returns_tokens(client):
    resp = await client.post("/api/v1/auth/dev-login", json={"username": "alice"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["access_token"]
    assert data["refresh_token"]
    assert data["token_type"] == "bearer"

    # 用返回的 token 能拿到本人信息
    me = await client.get("/api/v1/auth/me",
                          headers={"Authorization": f"Bearer {data['access_token']}"})
    assert me.status_code == 200, me.text
    assert me.json()["username"] == "alice"

    # 用户已建，余额 = initial_balance 回落默认 100
    async with async_session_maker() as s:
        u = (await s.execute(select(User).where(User.username == "alice"))).scalars().first()
    assert u is not None
    assert u.cash == Decimal("100")


@pytest.mark.asyncio
async def test_dev_login_first_user_is_admin(client):
    resp = await client.post("/api/v1/auth/dev-login", json={"username": "firstadmin"})
    assert resp.status_code == 200, resp.text
    async with async_session_maker() as s:
        u = (await s.execute(select(User).where(User.username == "firstadmin"))).scalars().first()
    assert u.is_superuser is True, "首个 dev-login 用户应自动成为超管"


@pytest.mark.asyncio
async def test_dev_login_idempotent_same_username(client):
    r1 = await client.post("/api/v1/auth/dev-login", json={"username": "bob"})
    r2 = await client.post("/api/v1/auth/dev-login", json={"username": "bob"})
    assert r1.status_code == 200 and r2.status_code == 200
    async with async_session_maker() as s:
        n = (await s.execute(
            select(func.count()).select_from(User).where(User.username == "bob")
        )).scalar_one()
    assert n == 1, "同名 dev-login 应复用同一用户，不重复创建"


@pytest.mark.asyncio
async def test_dev_login_404_in_production(client, monkeypatch):
    """生产环境必须拒绝（404），防 auth bypass 泄到 prod。"""
    monkeypatch.setattr(settings, "APP_ENV", "production")
    resp = await client.post("/api/v1/auth/dev-login", json={"username": "evil"})
    assert resp.status_code == 404, f"prod 下 dev-login 必须 404，实得 {resp.status_code}"
