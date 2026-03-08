import asyncio
import uuid
import sys
from httpx import AsyncClient, ASGITransport
from sqlmodel import SQLModel, select
from app.main import app
from app.core.database import engine, async_session_maker
from app.models.base import User, Market, Outcome, Position, Transaction

# 颜色控制
GREEN = "\033[92m"
RED = "\033[91m"
BLUE = "\033[94m"
YELLOW = "\033[93m"
RESET = "\033[0m"

async def init_db():
    print(f"{BLUE}正在初始化数据库...{RESET}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

async def create_identities(client: AsyncClient):
    """通过 API 注册管理员和普通用户，而不是直接操作数据库"""
    suffix = uuid.uuid4().hex[:4]
    
    # 1. 注册管理员
    admin_email = f"admin_{suffix}@gensokyo.gov"
    admin_payload = {
        "email": admin_email,
        "password": "admin123",
        "username": f"饭纲丸龙_{suffix}"
    }
    # 注意：注册接口默认是不给 superuser 权限的
    await client.post("/api/v1/auth/register", json=admin_payload)
    
    # 【关键步骤】我们需要去数据库里手动把这个账号提权为管理员
    async with async_session_maker() as session:
        from sqlalchemy import select
        res = await session.execute(select(User).where(User.email == admin_email))
        admin_db = res.scalar_one()
        admin_db.is_superuser = True
        await session.commit()

    # 2. 注册普通交易员
    user_email = f"marisa_{suffix}@kirisame.shop"
    user_payload = {
        "email": user_email,
        "password": "daze123",
        "username": f"雾雨魔理沙_{suffix}"
    }
    await client.post("/api/v1/auth/register", json=user_payload)
    
    print(f"{GREEN}身份创建完成：{admin_email} (管理员), {user_email} (普通用户){RESET}")
    return admin_email, user_email
async def test_flow():
    await init_db()

    
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin_email, user_email = await create_identities(client)
        # --- 1. 登录获取 Token ---
        print(f"{YELLOW}步骤 1: 身份验证{RESET}")
        
        # 管理员登录
        res_a = await client.post("/api/v1/auth/jwt/login", data={"username": admin_email, "password": "admin123"})
        admin_token = res_a.json()["access_token"]
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        
        # 普通用户登录
        res_u = await client.post("/api/v1/auth/jwt/login", data={"username": user_email, "password": "daze123"})
        user_token = res_u.json()["access_token"]
        user_headers = {"Authorization": f"Bearer {user_token}"}
        print(f"{GREEN}登录成功。{RESET}")

        # --- 2. 管理员创建市场 ---
        print(f"\n{YELLOW}步骤 2: 管理员创建多选项市场{RESET}")
        market_payload = {
            "title": "第143回御前试合",
            "description": "博丽灵梦 vs 雾雨魔理沙 vs 东风谷早苗",
            "liquidity_b": 100.0,
            "outcomes": ["博丽灵梦", "雾雨魔理沙", "东风谷早苗"]
        }
        res_m = await client.post("/api/v1/market/create", json=market_payload, headers=admin_headers)
        if res_m.status_code != 201:
            print(f"{RED}创建市场失败: {res_m.text}{RESET}")
            return
        market_id = res_m.json()["market_id"]
        print(f"{GREEN}市场创建成功，ID: {market_id}{RESET}")

        # --- 3. 普通用户查看价格 ---
        print(f"\n{YELLOW}步骤 3: 查看初始价格 (三方对决理论价格应为 0.3333){RESET}")
        res_l = await client.get("/api/v1/market/list")
        market = [m for m in res_l.json() if m["id"] == market_id][0]
        outcomes = market["outcomes"]
        for o in outcomes:
            print(f"选项: {o['label']} | 价格: {o['current_price']}")
        
        reimu_id = outcomes[0]["id"]

        # --- 4. 普通用户买入 ---
        print(f"\n{YELLOW}步骤 4: 普通用户买入 40 张 [博丽灵梦]{RESET}")
        res_b = await client.post("/api/v1/market/buy", headers=user_headers, json={
            "outcome_id": reimu_id,
            "shares": 40.0
        })
        b_data = res_b.json()
        print(f"{GREEN}买入成功! 花费: {b_data['cost']}, 剩余现金: {b_data['new_cash']}{RESET}")

        # --- 5. 检查滑点后的价格变动 ---
        print(f"\n{YELLOW}步骤 5: 检查价格变动 (灵梦价格应该上涨){RESET}")
        res_l2 = await client.get("/api/v1/market/list")
        market2 = [m for m in res_l2.json() if m["id"] == market_id][0]
        for o in market2["outcomes"]:
            print(f"选项: {o['label']} | 价格: {o['current_price']}")

        # --- 6. 卖出测试 (含手续费验证) ---
        print(f"\n{YELLOW}步骤 6: 卖出 20 张 [博丽灵梦] 并验证 1% 手续费{RESET}")
        res_s = await client.post("/api/v1/market/sell", headers=user_headers, json={
            "outcome_id": reimu_id,
            "shares": 20.0
        })
        s_data = res_s.json()
        # 卖出时 cost 为负值（代表现金流入用户）
        print(f"{GREEN}卖出成功! 净收入: {abs(s_data['cost'])}, 剩余现金: {s_data['new_cash']}{RESET}")

        # --- 7. 安全测试: 普通用户尝试创建市场 ---
        print(f"\n{YELLOW}步骤 7: 安全测试 (普通用户尝试创建市场应被拦截){RESET}")
        res_illegal = await client.post("/api/v1/market/create", json=market_payload, headers=user_headers)
        if res_illegal.status_code == 403:
            print(f"{GREEN}防御成功: 普通用户被禁止创建市场 (403 Forbidden){RESET}")
        else:
            print(f"{RED}安全漏洞! 普通用户成功创建了市场!{RESET}")

    print(f"\n{BLUE}=== 核心市场功能测试完成 ==={RESET}")

if __name__ == "__main__":
    asyncio.run(test_flow())