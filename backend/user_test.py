# TODO: 此测试依赖旧的 fastapi-users 注册/登录/PATCH 端点，
# 现在认证已改为 Casdoor OAuth2，需要重写。
#
# 旧测试覆盖的场景（仍有价值，需用新方式实现）：
# 1. 恶意注册提权 → Casdoor 化后不再适用（注册由 Casdoor 控制）
# 2. 登录获取 Token → 现在通过 /auth/callback 换 JWT
# 3. PATCH /me 充值 → 该端点已移除
#
# 新测试建议：
# - 直接创建用户 + create_access_token() 签发 JWT
# - 测试 /user/summary、/user/holdings、/user/transactions
# - 测试 refresh token 机制

import asyncio
import uuid
from decimal import Decimal
from httpx import AsyncClient, ASGITransport
from sqlmodel import SQLModel
from app.main import app
from app.core.database import engine, async_session_maker
from app.core.users import create_access_token
from app.models.base import User

GREEN = "\033[92m"
RED = "\033[91m"
BLUE = "\033[94m"
YELLOW = "\033[93m"
RESET = "\033[0m"


async def test_suite():
    # 初始化数据库
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    # 创建测试用户
    suffix = uuid.uuid4().hex[:4]
    async with async_session_maker() as session:
        async with session.begin():
            user = User(
                username=f"marisa_{suffix}",
                email=f"marisa_{suffix}@kirisame.shop",
                casdoor_id=f"test_{suffix}",
                cash=Decimal("100"),
                debt=Decimal("0"),
            )
            session.add(user)
            await session.flush()
            user_id = user.id

    token = create_access_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # --- 1. 获取用户信息 ---
        print(f"{YELLOW}测试 [1/4]: 获取当前用户信息 (/auth/me){RESET}")
        me_res = await client.get("/api/v1/auth/me", headers=headers)
        assert me_res.status_code == 200
        me_data = me_res.json()
        assert me_data["cash"] == 100.0
        print(f"{GREEN}  通过: cash={me_data['cash']}, username={me_data['username']}{RESET}")

        # --- 2. 获取资产概览 ---
        print(f"{YELLOW}测试 [2/4]: 获取资产概览 (/user/summary){RESET}")
        summary_res = await client.get("/api/v1/user/summary", headers=headers)
        assert summary_res.status_code == 200
        summary = summary_res.json()
        assert summary["cash"] == 100.0
        assert summary["holdings_value"] == 0.0
        print(f"{GREEN}  通过: net_worth={summary['net_worth']}, rank={summary['rank']}{RESET}")

        # --- 3. 获取持仓（应为空） ---
        print(f"{YELLOW}测试 [3/4]: 获取持仓明细 (/user/holdings){RESET}")
        holdings_res = await client.get("/api/v1/user/holdings", headers=headers)
        assert holdings_res.status_code == 200
        assert holdings_res.json() == []
        print(f"{GREEN}  通过: 持仓为空{RESET}")

        # --- 4. 获取交易历史（应为空） ---
        print(f"{YELLOW}测试 [4/4]: 获取交易历史 (/user/transactions){RESET}")
        tx_res = await client.get("/api/v1/user/transactions", headers=headers)
        assert tx_res.status_code == 200
        assert tx_res.json() == []
        print(f"{GREEN}  通过: 交易历史为空{RESET}")

    print(f"\n{BLUE}=== 用户接口测试完成 ==={RESET}")


if __name__ == "__main__":
    asyncio.run(test_suite())
