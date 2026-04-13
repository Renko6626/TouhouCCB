# TODO: 此测试依赖旧的 fastapi-users 注册/登录端点，
# 现在认证已改为 Casdoor OAuth2，需要重写。
#
# 重写方向：
# 1. 直接操作数据库创建测试用户（绕过 Casdoor）
# 2. 用 create_access_token() 手动签发 JWT
# 3. 测试市场创建、买入、卖出、结算的完整流程
#
# 示例框架：
#   from app.core.users import create_access_token
#   token = create_access_token(user.id)
#   headers = {"Authorization": f"Bearer {token}"}

import asyncio
import uuid
from decimal import Decimal
from httpx import AsyncClient, ASGITransport
from sqlmodel import SQLModel
from app.main import app
from app.core.database import engine, async_session_maker
from app.core.users import create_access_token
from app.models.base import User, Market, Outcome, MarketStatus

GREEN = "\033[92m"
RED = "\033[91m"
BLUE = "\033[94m"
YELLOW = "\033[93m"
RESET = "\033[0m"


async def init_db():
    print(f"{BLUE}正在初始化数据库...{RESET}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def create_test_users():
    """直接在数据库中创建测试用户，绕过 Casdoor"""
    suffix = uuid.uuid4().hex[:4]
    async with async_session_maker() as session:
        async with session.begin():
            admin = User(
                username=f"饭纲丸龙_{suffix}",
                email=f"admin_{suffix}@gensokyo.gov",
                casdoor_id=f"test_admin_{suffix}",
                is_superuser=True,
                cash=Decimal("100000"),
                debt=Decimal("0"),
            )
            user = User(
                username=f"雾雨魔理沙_{suffix}",
                email=f"marisa_{suffix}@kirisame.shop",
                casdoor_id=f"test_user_{suffix}",
                is_superuser=False,
                cash=Decimal("100"),
                debt=Decimal("0"),
            )
            session.add(admin)
            session.add(user)
            await session.flush()
            admin_id, user_id = admin.id, user.id

    admin_token = create_access_token(admin_id)
    user_token = create_access_token(user_id)
    print(f"{GREEN}测试用户创建完成{RESET}")
    return (
        {"Authorization": f"Bearer {admin_token}"},
        {"Authorization": f"Bearer {user_token}"},
    )


async def test_flow():
    await init_db()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin_headers, user_headers = await create_test_users()

        # --- 1. 管理员创建市场 ---
        print(f"\n{YELLOW}步骤 1: 管理员创建多选项市场{RESET}")
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

        # --- 2. 查看初始价格 ---
        print(f"\n{YELLOW}步骤 2: 查看初始价格 (三方对决理论价格应为 0.3333){RESET}")
        res_l = await client.get("/api/v1/market/list")
        market = [m for m in res_l.json() if m["id"] == market_id][0]
        outcomes = market["outcomes"]
        for o in outcomes:
            print(f"  选项: {o['label']} | 价格: {o['current_price']}")
        reimu_id = outcomes[0]["id"]

        # --- 3. 普通用户买入 ---
        print(f"\n{YELLOW}步骤 3: 普通用户买入 40 张 [博丽灵梦]{RESET}")
        res_b = await client.post("/api/v1/market/buy", headers=user_headers, json={
            "outcome_id": reimu_id,
            "shares": 40.0
        })
        b_data = res_b.json()
        print(f"{GREEN}买入成功! 花费: {b_data['cost']}, 剩余现金: {b_data['new_cash']}{RESET}")

        # --- 4. 检查价格变动 ---
        print(f"\n{YELLOW}步骤 4: 检查价格变动 (灵梦价格应该上涨){RESET}")
        res_l2 = await client.get("/api/v1/market/list")
        market2 = [m for m in res_l2.json() if m["id"] == market_id][0]
        for o in market2["outcomes"]:
            print(f"  选项: {o['label']} | 价格: {o['current_price']}")

        # --- 5. 卖出测试 ---
        print(f"\n{YELLOW}步骤 5: 卖出 20 张 [博丽灵梦]{RESET}")
        res_s = await client.post("/api/v1/market/sell", headers=user_headers, json={
            "outcome_id": reimu_id,
            "shares": 20.0
        })
        s_data = res_s.json()
        print(f"{GREEN}卖出成功! 净收入: {abs(s_data['cost'])}, 剩余现金: {s_data['new_cash']}{RESET}")

        # --- 6. 安全测试: 普通用户尝试创建市场 ---
        print(f"\n{YELLOW}步骤 6: 安全测试 (普通用户尝试创建市场应被拦截){RESET}")
        res_illegal = await client.post("/api/v1/market/create", json=market_payload, headers=user_headers)
        if res_illegal.status_code == 403:
            print(f"{GREEN}防御成功: 普通用户被禁止创建市场 (403 Forbidden){RESET}")
        else:
            print(f"{RED}安全漏洞! 普通用户成功创建了市场!{RESET}")

    print(f"\n{BLUE}=== 核心市场功能测试完成 ==={RESET}")


if __name__ == "__main__":
    asyncio.run(test_flow())
