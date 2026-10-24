"""Validate and seed current site configuration without changing the schema."""
from __future__ import annotations

from sqlalchemy import text
from sqlmodel import select

from app.core.database import async_session_maker
from app.core.config import settings
from app.services.credit.flags import DEFAULT_CREDIT_LEVERAGE, DEFAULT_CREDIT_MAINTENANCE_RATIO
from app.services.site_config import FX_DEFAULT_CONFIGS

DEFAULT_CREDIT_RISK_RETRY_LIMIT = 3
CREDIT_LEVERAGE_KEY = "credit_leverage"
CREDIT_MAINTENANCE_KEY = "credit_maintenance_ratio"

DEFAULT_CONFIGS = [
    ("site_access_disabled", "false", "bool"),
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
    # 统一信贷的运营冻结与重试配置。
    ("credit_new_risk_frozen", "false", "bool"),
    ("credit_risk_retry_limit", str(DEFAULT_CREDIT_RISK_RETRY_LIMIT), "int"),
]

# Keep all startup defaults in one seed operation so existing installations
# receive the FX keys without overwriting operator changes.
DEFAULT_CONFIGS.extend(FX_DEFAULT_CONFIGS)


# Current risk parameters are part of the declared configuration collection.
DEFAULT_CONFIGS.extend([
    (CREDIT_LEVERAGE_KEY, str(DEFAULT_CREDIT_LEVERAGE), "decimal"),
    (CREDIT_MAINTENANCE_KEY, str(DEFAULT_CREDIT_MAINTENANCE_RATIO), "decimal"),
])


def resolve_credit_config(raw: dict[str, str]) -> dict[str, str]:
    """Preserve valid current parameters; only an empty store gets defaults."""
    from app.services.credit.thresholds import validate_thresholds

    values = {}
    for key, default in (
        (CREDIT_LEVERAGE_KEY, str(DEFAULT_CREDIT_LEVERAGE)),
        (CREDIT_MAINTENANCE_KEY, str(DEFAULT_CREDIT_MAINTENANCE_RATIO)),
    ):
        if key in raw:
            values[key] = raw[key]
        elif not raw:
            values[key] = default
        else:
            raise ValueError(f"Missing credit configuration: {key}")
    validate_thresholds(values[CREDIT_LEVERAGE_KEY], values[CREDIT_MAINTENANCE_KEY])
    return values


async def _seed_configs(*, all_defaults: bool) -> list[str]:
    from app.models.base import SiteConfig
    from app.services import audit_service

    seeded = []
    async with async_session_maker() as session:
        async with session.begin():
            rows = {row.key: row for row in (await session.execute(select(SiteConfig))).scalars()}
            values = resolve_credit_config({key: row.value for key, row in rows.items()})
            defaults = DEFAULT_CONFIGS if all_defaults else [
                (key, value, "decimal") for key, value in values.items()
            ]
            for key, value, value_type in defaults:
                if key not in rows:
                    # SQL conflict handling keeps concurrent seeders idempotent.
                    result = await session.execute(text(
                        "INSERT INTO siteconfig (key, value, value_type, updated_at) "
                        "VALUES (:key, :value, :value_type, CURRENT_TIMESTAMP) "
                        "ON CONFLICT (key) DO NOTHING RETURNING key"
                    ), {"key": key, "value": value, "value_type": value_type})
                    if result.scalar_one_or_none() is not None:
                        seeded.append(key)
                        if key in values:
                            audit_service.record(session, "config_set", payload={
                                "key": key, "old": None, "new": value,
                                "value_type": value_type, "source": "config_seed",
                            })
    return seeded


async def seed_current_config() -> list[str]:
    """Validate risk parameters before atomically filling current defaults."""
    return await _seed_configs(all_defaults=True)


async def seed_credit_risk_configs() -> list[str]:
    """Validate and fill current credit parameters without legacy conversion."""
    return await _seed_configs(all_defaults=False)
