"""LoanV1 启动期幂等迁移。

由 FastAPI lifespan 在 init_db() 之后、start_scheduler() 之前调用。
- 给现有 user 表补 debt_last_accrued_at 列（SQLModel create_all 不给已存在表加列）
- 给 siteconfig 表插入 4 条默认配置（表由 create_all 建好，但无默认行）

幂等：
- Postgres 用 ADD COLUMN IF NOT EXISTS
- SQLite 先查 PRAGMA table_info，不存在才 ADD COLUMN
- INSERT 用 ON CONFLICT (key) DO NOTHING（Postgres + SQLite 3.24+ 都支持）

Postgres 下 updated_at NOT NULL 但 SQLModel 没设 DB 默认值，所以 INSERT 必须显式给 CURRENT_TIMESTAMP。
"""
from __future__ import annotations
import logging
from sqlalchemy import text

from app.core.database import engine

logger = logging.getLogger("thccb.loan_migrate")


DEFAULT_CONFIGS = [
    ("loan_enabled", "true", "bool"),
    ("loan_leverage_k", "1.0", "decimal"),
    ("loan_daily_rate", "0.01", "decimal"),
    ("loan_sweep_interval_sec", "60", "int"),
]


async def auto_migrate() -> None:
    dialect = engine.dialect.name  # 'postgresql' | 'sqlite' | ...

    async with engine.begin() as conn:
        # 1. 补 user.debt_last_accrued_at 列
        if dialect == "postgresql":
            await conn.execute(text(
                'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS debt_last_accrued_at TIMESTAMPTZ NULL'
            ))
        elif dialect == "sqlite":
            result = await conn.execute(text('PRAGMA table_info("user")'))
            cols = {row[1] for row in result.fetchall()}
            if "debt_last_accrued_at" not in cols:
                await conn.execute(text(
                    'ALTER TABLE "user" ADD COLUMN debt_last_accrued_at DATETIME'
                ))
        else:
            logger.warning("auto_migrate: unsupported dialect %s, skip column add", dialect)

        # 2. 种默认 siteconfig
        # updated_at 显式用 CURRENT_TIMESTAMP（Postgres 和 SQLite 均支持）。
        for k, v, t in DEFAULT_CONFIGS:
            await conn.execute(
                text(
                    "INSERT INTO siteconfig (key, value, value_type, updated_at) "
                    "VALUES (:k, :v, :t, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (key) DO NOTHING"
                ),
                {"k": k, "v": v, "t": t},
            )

        # 3. 兜底：debt > 0 但 last_accrued_at 为空（防御性）
        if dialect == "postgresql":
            await conn.execute(text(
                'UPDATE "user" SET debt_last_accrued_at = NOW() '
                'WHERE debt > 0 AND debt_last_accrued_at IS NULL'
            ))
        elif dialect == "sqlite":
            await conn.execute(text(
                'UPDATE "user" SET debt_last_accrued_at = CURRENT_TIMESTAMP '
                'WHERE debt > 0 AND debt_last_accrued_at IS NULL'
            ))

    logger.info("loan auto-migrate done (dialect=%s)", dialect)
