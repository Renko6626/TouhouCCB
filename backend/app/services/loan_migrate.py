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
from app.core.config import settings

logger = logging.getLogger("thccb.loan_migrate")


DEFAULT_CONFIGS = [
    ("loan_enabled", "true", "bool"),
    ("loan_leverage_k", "1.0", "decimal"),
    ("loan_daily_rate", "0.01", "decimal"),
    ("loan_sweep_interval_sec", "60", "int"),
    ("liquidation_enabled", "false", "bool"),   # 默认关，灰度开启
    ("liquidation_sweep_interval_sec", "600", "int"),   # 10 min
    ("liquidation_hard_threshold", "0.2", "decimal"),
    ("liquidation_soft_threshold", "0.5", "decimal"),
    # ── Partial Liquidation ──
    ("liquidation_partial_pct", "0.10", "decimal"),
    ("liquidation_target_margin", "0.30", "decimal"),
    ("liquidation_emergency_threshold", "0.05", "decimal"),
    # ── Anti-bot (spec 2026-05-20-anti-bot-design.md) ──
    ("activity_mode_enabled", "false", "bool"),
    ("quant_whitelist_user_ids", "", "string"),
    ("bot_detection_enabled", "true", "bool"),
    ("bot_detection_interval_sec", "1800", "int"),
    ("bot_detection_window_sec", "7200", "int"),
    ("bot_freq_threshold", "120", "int"),
    ("bot_late_night_threshold", "20", "int"),
    ("bot_interval_stddev_ms_threshold", "100", "int"),
    ("bot_fast_follow_trigger_cost", "500.0", "decimal"),
    ("bot_fast_follow_latency_ms", "1000", "int"),
    ("bot_fast_follow_count_threshold", "3", "int"),
    # ── 经济参数（admin 热配）──
    ("sell_fee_rate", "0", "decimal"),                         # 卖出手续费率，默认 0
    ("initial_balance", str(settings.INITIAL_BALANCE), "decimal"),  # 新用户初始余额
    # ── 单写者重构（spec 2026-08-21）──
    # 终态默认（2026-08-22：二期未开、无在线用户，跳过灰度直接上终态；
    # 已有 DB 行不受种子影响，翻转仍按各自语义：writer 需重启，legacy 热生效）
    ("single_writer_enabled", "true", "bool"),    # 翻转需重启进程（启动时读一次）
    ("legacy_trade_events", "false", "bool"),     # 老 SSE 事件双发关闭（bot 已内建 tick 适配）；阶段 5 删
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
