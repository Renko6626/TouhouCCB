import asyncio
import uuid
import sys
from httpx import AsyncClient, ASGITransport
from app.main import app  # 确保路径正确指向你的 FastAPI 实例

# 颜色控制字符（让输出好看点）
GREEN = "\033[92m"
RED = "\033[91m"
BLUE = "\033[94m"
RESET = "\033[0m"
from app.models.base import User, Market, Outcome, Position, Transaction 

async def test_suite():
    # 强制初始化数据库
    async with engine.begin() as conn:
        # 这一步会确保执行时数据库里一定有表
        await conn.run_sync(SQLModel.metadata.create_all)
    
async def test_suite():
    print(f"{BLUE}=== 开始执行 东方炒炒币 安全防御测试脚本 ==={RESET}\n")

    # 使用 ASGITransport 直接连接 app，无需启动真正的服务器端口
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        
        unique_suffix = str(uuid.uuid4())[:6]
        test_email = f"marisa_{unique_suffix}@gensokyo.com"
        test_pass = "daze123456"
        test_user = f"魔理沙_{unique_suffix}"

        # --- 1. 正常注册测试 ---
        print(f"测试 [1/5]: 正常用户注册...", end=" ")
        reg_res = await client.post("/api/v1/auth/register", json={
            "email": test_email,
            "password": test_pass,
            "username": test_user
        })
        if reg_res.status_code == 201:
            print(f"{GREEN}通过{RESET}")
        else:
            print(f"{RED}失败: {reg_res.text}{RESET}")
            return

        # --- 2. 恶意注册提权测试 ---
        print(f"测试 [2/5]: 恶意请求拦截 (尝试注册时设为管理员和无限现金)...", end=" ")
        malicious_id = str(uuid.uuid4())[:6]
        malicious_res = await client.post("/api/v1/auth/register", json={
            "email": f"hacker_{malicious_id}@scam.com",
            "password": "password",
            "username": f"黑客_{malicious_id}",
            "is_superuser": True,  # 恶意注入
            "cash": 9999999.0,     # 恶意注入
            "is_verified": True    # 恶意注入
        })
        m_data = malicious_res.json()
        # 验证防御逻辑：字段是否被 UserManager 强制重置
        if m_data.get("is_superuser") is False and m_data.get("cash") == 100.0:
            print(f"{GREEN}防御成功{RESET}")
        else:
            print(f"{RED}防御失效! 用户成功篡改了敏感字段: {m_data}{RESET}")

        # --- 3. 登录并获取 Token ---
        print(f"测试 [3/5]: 用户登录获取 JWT Token...", end=" ")
        login_res = await client.post("/api/v1/auth/jwt/login", data={
            "username": test_email,
            "password": test_pass
        })
        if login_res.status_code == 200:
            token = login_res.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            print(f"{GREEN}通过{RESET}")
        else:
            print(f"{RED}失败: {login_res.text}{RESET}")
            return

        # --- 4. 正常访问个人信息 ---
        print(f"测试 [4/5]: 使用 Token 访问受保护接口 (/me)...", end=" ")
        me_res = await client.get("/api/v1/auth/me", headers=headers)
        if me_res.status_code == 200:
            print(f"{GREEN}通过{RESET}")
        else:
            print(f"{RED}失败{RESET}")

        # --- 5. 恶意更新现金测试 ---
        print(f"测试 [5/5]: 恶意请求拦截 (登录用户尝试 PATCH 接口给自己充值)...", end=" ")
        update_res = await client.patch("/api/v1/auth/me", headers=headers, json={
            "cash": 888888.0,
            "is_superuser": True,
            "username": "魔改后的魔理沙"
        })
        u_data = update_res.json()
        # 验证：用户名允许修改，但现金和权限必须被过滤掉
        if u_data.get("username") == "魔改后的魔理沙" and u_data.get("cash") == 100.0 and u_data.get("is_superuser") is False:
            print(f"{GREEN}防御成功{RESET}")
        else:
            print(f"{RED}防御失效! 用户绕过过滤修改了现金或权限: {u_data}{RESET}")

    print(f"\n{BLUE}=== 所有测试执行完毕 ==={RESET}")

if __name__ == "__main__":
    try:
        asyncio.run(test_suite())
    except KeyboardInterrupt:
        sys.exit(0)