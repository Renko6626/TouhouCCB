"""LoanV1 幂等迁移脚本。
- User.debt_last_accrued_at 列
- SiteConfig 表 + 默认配置
- 防御性兜底：debt > 0 但 last_accrued_at 为空的用户补 now()
运行：python -m scripts.migrate_loan_v1（在 backend/ 目录，激活 venv 后）
"""
import asyncio
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy import text
from app.core.database import engine


DEFAULT_CONFIGS = [
    ("loan_enabled", "true", "bool"),
    ("loan_leverage_k", "1.0", "decimal"),
    ("loan_daily_rate", "0.01", "decimal"),
    ("loan_sweep_interval_sec", "60", "int"),
]


async def run():
    async with engine.begin() as conn:
        # 1. User 加列
        await conn.execute(text(
            'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS debt_last_accrued_at TIMESTAMPTZ NULL'
        ))

        # 2. SiteConfig 表
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS siteconfig (
                id SERIAL PRIMARY KEY,
                key VARCHAR(64) NOT NULL,
                value TEXT NOT NULL,
                value_type VARCHAR(16) NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_by INTEGER REFERENCES "user"(id)
            )
        """))
        await conn.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_siteconfig_key ON siteconfig(key)'
        ))

        # 3. 默认配置
        for k, v, t in DEFAULT_CONFIGS:
            await conn.execute(
                text("INSERT INTO siteconfig (key, value, value_type) VALUES (:k, :v, :t) ON CONFLICT (key) DO NOTHING"),
                {"k": k, "v": v, "t": t},
            )

        # 4. 兜底
        await conn.execute(text(
            'UPDATE "user" SET debt_last_accrued_at = NOW() WHERE debt > 0 AND debt_last_accrued_at IS NULL'
        ))

    print("migrate_loan_v1: done")


if __name__ == "__main__":
    asyncio.run(run())
