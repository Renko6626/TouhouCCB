
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
import asyncio
from httpx import AsyncClient
from app.main import app
import pytest_asyncio
import uuid
from app.models.base import User, Market, Outcome, Position, Transaction 


from app.core.database import engine
from sqlmodel import SQLModel

async def test_suite():
    # 强制初始化数据库
    async with engine.begin() as conn:
        # 这一步会确保执行时数据库里一定有表
        await conn.run_sync(SQLModel.metadata.create_all)
    
# 设置基础 URL
BASE_URL = "http://localhost:8004"

@pytest_asyncio.fixture
async def client():
    from asgi_lifespan import LifespanManager
    async with LifespanManager(app):
        async with AsyncClient(base_url=BASE_URL, app=app) as ac:
            yield ac

# --- 1. 正常注册流程测试 ---
@pytest.mark.asyncio
async def test_register_success(client):
    unique_id = str(uuid.uuid4())[:8]
    payload = {
        "email": f"reimu_{unique_id}@hakurei.com",
        "password": "donations_please",
        "username": f"博丽灵梦_{unique_id}"
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    
    assert response.status_code == 201
    data = response.json()
    assert data["username"] == payload["username"]
    assert data["cash"] == 100.0  # 验证初始资金
    assert data["is_superuser"] is False # 验证默认权限

# --- 2. 恶意注册测试：尝试提权和刷钱 ---
@pytest.mark.asyncio
async def test_register_malicious_defense(client):
    unique_id = str(uuid.uuid4())[:8]
    payload = {
        "email": f"hacker_{unique_id}@scam.com",
        "password": "password123",
        "username": f"非法转世者_{unique_id}",
        # 恶意注入字段
        "is_superuser": True, 
        "cash": 9999999.0,
        "is_verified": True
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    
    assert response.status_code == 201
    data = response.json()
    
    # 【核心防御验证】
    # 即使请求里带了 true，后端 UserManager(safe=True) 应该强行将其抹除
    assert data["is_superuser"] is False 
    assert data["cash"] == 100.0
    assert data["is_verified"] is False
    print("\n✅ 防御成功：恶意注册提权请求已被拦截并修正。")

# --- 3. 登录并获取 Token ---
@pytest.mark.asyncio
async def test_login_and_get_me(client):
    # 先注册一个
    email = f"marisa_{uuid.uuid4().hex[:4]}@kirisame.shop"
    password = "daze"
    await client.post("/api/v1/auth/register", json={
        "email": email, "password": password, "username": "雾雨魔理沙"
    })

    # 登录 (FastAPI-Users 默认使用 Form 表单格式登录)
    login_data = {
        "username": email,
        "password": password
    }
    login_res = await client.post("/api/v1/auth/jwt/login", data=login_data)
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 测试查看个人信息
    me_res = await client.get("/api/v1/auth/me", headers=headers)
    assert me_res.status_code == 200
    assert me_res.json()["username"] == "雾雨魔理沙"

# --- 4. 恶意更新测试：普通用户尝试给自己充值 ---
@pytest.mark.asyncio
async def test_update_malicious_defense(client):
    # 1. 注册并登录
    email = f"test_{uuid.uuid4().hex[:4]}@test.com"
    password = "password"
    await client.post("/api/v1/auth/register", json={
        "email": email, "password": password, "username": "测试员"
    })
    login_res = await client.post("/api/v1/auth/jwt/login", data={"username": email, "password": password})
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 2. 尝试 PATCH /me 接口修改自己的 cash 和权限
    malicious_update = {
        "cash": 50000.0,
        "is_superuser": True,
        "username": "魔改测试员"
    }
    update_res = await client.patch("/api/v1/auth/me", json=malicious_update, headers=headers)
    
    # 验证响应
    data = update_res.json()
    assert data["username"] == "魔改测试员" # 普通字段允许修改
    assert data["cash"] == 100.0           # 【防御成功】现金未变
    assert data["is_superuser"] is False   # 【防御成功】权限未变
    print("\n✅ 防御成功：普通用户尝试通过 PATCH 修改敏感字段已被拦截。")
