"""Idempotent startup migration for loan fields and current site configuration.

Credit parameters are validated before other defaults are seeded. Missing
parameters map once from valid legacy values; existing gates remain intact.
"""
from __future__ import annotations
import logging
from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from sqlmodel import select

from app.core.database import async_session_maker, engine
from app.core.config import settings
from app.services.credit.flags import DEFAULT_CREDIT_LEVERAGE, DEFAULT_CREDIT_MAINTENANCE_RATIO
from app.services.site_config import FX_DEFAULT_CONFIGS

logger = logging.getLogger("thccb.loan_migrate")

#: 重复次数默认（计划 §3.4 credit_risk_retry_limit=3）。
DEFAULT_CREDIT_RISK_RETRY_LIMIT = 3

LEGACY_LEVERAGE_KEY = "loan_leverage_k"
LEGACY_HARD_THRESHOLD_KEY = "liquidation_hard_threshold"
CREDIT_LEVERAGE_KEY = "credit_leverage"
CREDIT_MAINTENANCE_KEY = "credit_maintenance_ratio"
CREDIT_MIGRATION_SOURCE = "credit_migration"


DEFAULT_CONFIGS = [
    ("homepage_fx_enabled", "true", "bool"),  # 仅首页展示，保留管理员已有选择
    ("loan_enabled", "true", "bool"),
    ("loan_daily_rate", "0.01", "decimal"),
    ("loan_sweep_interval_sec", "60", "int"),
    ("loan_sweep_min_accrual_sec", "3600", "int"),   # 定时结息折叠窗口（审计 M1）
    ("liquidation_enabled", "false", "bool"),   # 默认关，灰度开启
    ("liquidation_sweep_interval_sec", "600", "int"),   # 10 min
    # ── Partial Liquidation ──
    ("liquidation_partial_pct", "0.10", "decimal"),
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
    # 保留已有配置与默认值；统一信贷下，取得写所有权的实例始终启动 writer。
    ("single_writer_enabled", "true", "bool"),
    ("legacy_trade_events", "false", "bool"),     # 老 SSE 事件双发关闭（bot 已内建 tick 适配）；阶段 5 删
    # 统一信贷的运营冻结与重试配置。
    ("credit_new_risk_frozen", "false", "bool"),
    ("credit_risk_retry_limit", str(DEFAULT_CREDIT_RISK_RETRY_LIMIT), "int"),
]

# Keep all startup defaults in one seed operation so existing installations
# receive the FX keys without overwriting operator changes.
DEFAULT_CONFIGS.extend(FX_DEFAULT_CONFIGS)


async def auto_migrate() -> None:
    await seed_credit_risk_configs()
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


OBSOLETE_CONFIG_KEYS = (
    "unified_credit_enabled", "loan_leverage_k", "liquidation_hard_threshold",
    "liquidation_soft_threshold", "liquidation_target_margin", "liquidation_emergency_threshold",
)


def resolve_credit_config(raw: dict[str, str]) -> dict[str, str]:
    """Preserve explicit values; map missing parameters only from legacy values.

    Only a completely empty configuration receives fresh-install defaults.
    Invalid or incomplete existing configuration must be repaired explicitly.
    """
    from app.services.credit.thresholds import validate_thresholds

    values = {}
    for key, legacy, default in (
        (CREDIT_LEVERAGE_KEY, LEGACY_LEVERAGE_KEY, str(DEFAULT_CREDIT_LEVERAGE)),
        (CREDIT_MAINTENANCE_KEY, LEGACY_HARD_THRESHOLD_KEY, str(DEFAULT_CREDIT_MAINTENANCE_RATIO)),
    ):
        if key in raw:
            values[key] = raw[key]
        elif legacy in raw:
            try:
                candidate = Decimal(raw[legacy])
                if not candidate.is_finite():
                    raise ValueError(f"{legacy} must be finite")
                values[key] = format(candidate + 1 if key == CREDIT_LEVERAGE_KEY else candidate, "f")
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"Invalid legacy credit configuration: {legacy}") from exc
        elif not raw:
            values[key] = default
        else:
            raise ValueError(f"Missing credit configuration: {key}")
    validate_thresholds(values[CREDIT_LEVERAGE_KEY], values[CREDIT_MAINTENANCE_KEY])
    return values


async def seed_credit_risk_configs() -> list[str]:
    """Validate, migrate missing parameters and remove obsolete keys atomically."""
    from app.models.base import SiteConfig
    from app.services import audit_service
    from sqlalchemy import delete

    seeded = []
    async with async_session_maker() as session:
        async with session.begin():
            rows = {row.key: row for row in (await session.execute(select(SiteConfig))).scalars()}
            values = resolve_credit_config({key: row.value for key, row in rows.items()})
            for key, value in values.items():
                if key not in rows:
                    session.add(SiteConfig(key=key, value=value, value_type="decimal"))
                    seeded.append(key)
                    audit_service.record(session, "config_set", payload={
                        "key": key, "old": None, "new": value,
                        "value_type": "decimal", "source": CREDIT_MIGRATION_SOURCE,
                    })
            await session.execute(delete(SiteConfig).where(SiteConfig.key.in_(OBSOLETE_CONFIG_KEYS)))
    return seeded
