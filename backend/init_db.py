# init_db.py — 空库 Alembic 初始化及既有开发示例数据
# 用法：python init_db.py 或 docker compose exec backend python init_db.py

import asyncio
from pathlib import Path
from alembic.config import Config
from alembic import command
from app.core.schema import classify_schema

from app.core.database import engine, async_session_maker
from app.core.config import settings
from app.models.base import Market, Outcome, MarketStatus
from app.models import redemption as _redemption_models  # noqa: F401  注册兑换码/弹幕表到 SQLModel.metadata
from app.models import title as _title_models  # noqa: F401  注册称号系统表到 SQLModel.metadata
from app.models import ledger as _ledger_models  # noqa: F401  注册资金流水账本表到 SQLModel.metadata
from app.models import audit as _audit_models  # noqa: F401  注册审计事件流表到 SQLModel.metadata
# 保持开发入口的模型注册；实际 schema 完全由 Alembic 建立。
from app.models import fx as _fx_models  # noqa: F401  注册 FX 表到 SQLModel.metadata
from app.models import bot as _bot_models  # noqa: F401  注册 bot_profile 到 SQLModel.metadata
from app.models import credit as _credit_models  # noqa: F401  注册 liquidation_run/action 到 SQLModel.metadata


async def init_db():
    print("初始化空数据库；已有数据库将拒绝初始化")
    async with engine.begin() as conn:
        def bootstrap(connection):
            if classify_schema(connection) != "empty":
                raise RuntimeError("init_db requires an empty database")
            cfg = Config(str(Path(__file__).resolve().parent / "alembic.ini"))
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
        await conn.run_sync(bootstrap)

    # 开发环境沿用示例市场；生产环境只建立 schema。
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
