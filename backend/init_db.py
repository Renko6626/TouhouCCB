# init_db.py — 初始化数据库（建表 + 示例数据）
# 用法：python init_db.py 或 docker compose exec backend python init_db.py

import asyncio
from pathlib import Path
from sqlmodel import SQLModel
from sqlalchemy import text
from sqlalchemy.schema import DropTable
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.database import engine, async_session_maker
from app.core.config import settings
from app.models.base import Market, Outcome, MarketStatus
from app.models import redemption as _redemption_models  # noqa: F401  注册兑换码/弹幕表到 SQLModel.metadata
from app.models import title as _title_models  # noqa: F401  注册称号系统表到 SQLModel.metadata
from app.models import ledger as _ledger_models  # noqa: F401  注册资金流水账本表到 SQLModel.metadata


def get_alembic_head() -> str:
    """Return the Alembic head revision for the freshly-created schema."""
    alembic_ini = Path(__file__).resolve().parent / "alembic.ini"
    return ScriptDirectory.from_config(Config(str(alembic_ini))).get_current_head()


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

        # create_all 已经创建了当前模型定义下的完整 schema；
        # 标记 Alembic 到 head，避免后续 deploy.sh 再重复执行历史 add_column/create_table。
        print("-> STAMP ALEMBIC HEAD")
        head_revision = get_alembic_head()
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL)"
        ))
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:version_num)"),
            {"version_num": head_revision},
        )

    # 2. 开发环境插入示例数据；生产清库重开时保持真正空库。
    if settings.is_production:
        print("  生产环境：跳过示例市场创建")
    else:
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
