# scripts/init_db.py
import asyncio
from decimal import Decimal
from sqlmodel import SQLModel
from sqlalchemy import text
from sqlalchemy.schema import DropTable

from app.core.database import engine, async_session_maker
from app.core.config import settings
from app.models.base import User, Market, Outcome, MarketStatus


async def init_db():
    db_url = settings.build_db_url()
    is_sqlite = db_url.startswith("sqlite+aiosqlite://")

    print("====================================")
    print("  初始化数据库（会清空所有数据）")
    print("DATABASE:", db_url)
    print("====================================")

    confirm = input("确认清空数据库？输入 YES: ")
    if confirm != "YES":
        print("取消操作")
        return

    # ------------------------
    # 1. Drop & Create
    # ------------------------
    async with engine.begin() as conn:
        print("-> DROP ALL TABLES")
        if is_sqlite:
            await conn.execute(text("PRAGMA foreign_keys=OFF"))
        else:
            await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))

        for table in reversed(SQLModel.metadata.sorted_tables):
            await conn.execute(DropTable(table, if_exists=True))

        if is_sqlite:
            await conn.execute(text("PRAGMA foreign_keys=ON"))
        else:
            await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))

        print("-> CREATE ALL TABLES")
        await conn.run_sync(SQLModel.metadata.create_all)

    # ------------------------
    # 2. 插入初始数据
    # ------------------------
    async with async_session_maker() as db:
        async with db.begin():
            # 管理员（通过 Casdoor 首次登录时自动绑定 casdoor_id）
            admin = User(
                username="admin",
                email="admin@qq.com",
                is_active=True,
                is_superuser=True,
                cash=Decimal("100000"),
                debt=Decimal("0"),
            )
            db.add(admin)
            await db.flush()

            print(f"  管理员创建完成: {admin.username} (superuser)")
            print("  提示: 首次通过 Casdoor 登录后自动绑定账号")

            # 初始市场
            market = Market(
                title="灵梦 vs 魔理沙 谁会赢？",
                description="初始测试市场",
                liquidity_b=100.0,
                status=MarketStatus.TRADING,
            )
            db.add(market)
            await db.flush()

            outcomes = [
                Outcome(market_id=market.id, label="博丽灵梦"),
                Outcome(market_id=market.id, label="雾雨魔理沙"),
            ]
            for o in outcomes:
                db.add(o)

            print("  初始市场创建完成:", market.title)
            print("  outcomes:", [o.label for o in outcomes])

    print("====================================")
    print("  数据库初始化完成")
    print("====================================")


if __name__ == "__main__":
    asyncio.run(init_db())
