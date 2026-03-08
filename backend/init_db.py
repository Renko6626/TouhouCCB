# scripts/init_db.py
import asyncio
from sqlmodel import SQLModel
from sqlalchemy import text

from app.core.database import engine, async_session_maker
from app.core.config import settings
from app.models.base import User, Market, Outcome
from fastapi_users.password import PasswordHelper

password_helper = PasswordHelper()


async def init_db():
    print("====================================")
    print("⚠️  初始化数据库（会清空所有数据）")
    print("DATABASE:", settings.build_db_url())
    print("====================================")

    confirm = input("确认清空数据库？输入 YES: ")
    if confirm != "YES":
        print("取消操作")
        return

    # ------------------------
    # 1. Drop & Create
    # ------------------------
    async with engine.begin() as conn:
        print("→ DROP ALL TABLES")
        # MySQL: 避免循环外键导致 DROP FOREIGN KEY 失败
        await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for table in reversed(SQLModel.metadata.sorted_tables):
            await conn.execute(text(f"DROP TABLE IF EXISTS {table.name}"))
        await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))

        print("→ CREATE ALL TABLES")
        await conn.run_sync(SQLModel.metadata.create_all)

    # ------------------------
    # 2. 插入初始数据
    # ------------------------
    async with async_session_maker() as db:
        async with db.begin():
            # 管理员
            admin_email = "admin@gensokyo.local"
            admin_username = "yakumo_admin"
            admin_password = "admin123"

            hashed = password_helper.hash(admin_password)

            admin = User(
                email=admin_email,
                username=admin_username,
                hashed_password=hashed,
                is_active=True,
                is_superuser=True,
                is_verified=True,
                cash=100000.0,
                debt=0.0,
            )
            db.add(admin)
            await db.flush()

            print(f"✔ 管理员创建完成: {admin_username} / {admin_password}")

            # 初始市场
            market = Market(
                title="灵梦 vs 魔理沙 谁会赢？",
                description="初始测试市场",
                liquidity_b=100.0,
                status="trading",
            )
            db.add(market)
            await db.flush()

            outcomes = [
                Outcome(market_id=market.id, label="博丽灵梦"),
                Outcome(market_id=market.id, label="雾雨魔理沙"),
            ]
            for o in outcomes:
                db.add(o)

            print("✔ 初始市场创建完成:", market.title)
            print("  outcomes:", [o.label for o in outcomes])

    print("====================================")
    print("✅ 数据库初始化完成")
    print("====================================")


if __name__ == "__main__":
    asyncio.run(init_db())
