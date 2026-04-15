# init_db.py — 初始化数据库（建表 + 示例数据）
# 用法：python init_db.py 或 docker compose exec backend python init_db.py

import asyncio
from sqlmodel import SQLModel
from sqlalchemy import text
from sqlalchemy.schema import DropTable

from app.core.database import engine, async_session_maker
from app.core.config import settings
from app.models.base import Market, Outcome, MarketStatus


async def init_db():
    db_url = settings.build_db_url()
    is_sqlite = db_url.startswith("sqlite+aiosqlite://")

    print("====================================")
    print("  初始化数据库（会清空所有数据）")
    print("  DATABASE:", db_url.split("@")[-1] if "@" in db_url else db_url)
    print("====================================")

    confirm = input("确认清空数据库？输入 YES: ")
    if confirm != "YES":
        print("取消操作")
        return

    # 1. Drop & Create
    async with engine.begin() as conn:
        print("-> DROP ALL TABLES")
        if is_sqlite:
            await conn.execute(text("PRAGMA foreign_keys=OFF"))
            for table in reversed(SQLModel.metadata.sorted_tables):
                await conn.execute(DropTable(table, if_exists=True))
            await conn.execute(text("PRAGMA foreign_keys=ON"))
        else:
            # PostgreSQL / MySQL: CASCADE 处理外键依赖
            for table in reversed(SQLModel.metadata.sorted_tables):
                await conn.execute(text(f'DROP TABLE IF EXISTS "{table.name}" CASCADE'))

        print("-> CREATE ALL TABLES")
        await conn.run_sync(SQLModel.metadata.create_all)

    # 2. 插入示例数据
    async with async_session_maker() as db:
        async with db.begin():
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

            print("  示例市场创建完成:", market.title)

    print("")
    print("====================================")
    print("  数据库初始化完成")
    print("  第一个通过 SSO 登录的用户将自动成为管理员")
    print("====================================")


if __name__ == "__main__":
    asyncio.run(init_db())
