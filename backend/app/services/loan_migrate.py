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
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import text
from sqlmodel import select

from app.core.database import async_session_maker, engine
from app.core.config import settings
from app.services.site_config import FX_DEFAULT_CONFIGS

logger = logging.getLogger("thccb.loan_migrate")

#: 重复次数默认（计划 §3.4 credit_risk_retry_limit=3）。
DEFAULT_CREDIT_RISK_RETRY_LIMIT = 3
#: 名义杠杆上限（F6：20x 需运营显式启用；迁移不得把旧 k>19 直接映射成 >20x）。
MAX_CREDIT_LEVERAGE = Decimal("20")

LEGACY_LEVERAGE_KEY = "loan_leverage_k"
LEGACY_HARD_THRESHOLD_KEY = "liquidation_hard_threshold"
CREDIT_LEVERAGE_KEY = "credit_leverage"
CREDIT_MAINTENANCE_KEY = "credit_maintenance_ratio"
CREDIT_MIGRATION_SOURCE = "credit_migration"


DEFAULT_CONFIGS = [
    ("homepage_fx_enabled", "true", "bool"),  # 仅首页展示，保留管理员已有选择
    ("loan_enabled", "true", "bool"),
    ("loan_leverage_k", "1.0", "decimal"),
    ("loan_daily_rate", "0.01", "decimal"),
    ("loan_sweep_interval_sec", "60", "int"),
    ("loan_sweep_min_accrual_sec", "3600", "int"),   # 定时结息折叠窗口（审计 M1）
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
    # ── 统一信贷风险（计划 §3.4）──
    # 总开关默认 false：开关关着时后续 WP 的落地对现有交易零行为变化。
    # credit_leverage / credit_maintenance_ratio 不在这里种静态值，由
    # _seed_credit_risk_configs() 按 F7/F6 从旧配置派生（见下）。
    ("unified_credit_enabled", "false", "bool"),
    ("credit_new_risk_frozen", "false", "bool"),
    ("credit_risk_retry_limit", str(DEFAULT_CREDIT_RISK_RETRY_LIMIT), "int"),
]

# Keep all startup defaults in one seed operation so existing installations
# receive the FX keys without overwriting operator changes.
DEFAULT_CONFIGS.extend(FX_DEFAULT_CONFIGS)


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

    # 4. 统一信贷风险：从旧配置派生 credit_leverage / credit_maintenance_ratio。
    await seed_credit_risk_configs()

    logger.info("loan auto-migrate done (dialect=%s)", dialect)


def _decimal_or_none(raw: Optional[str]) -> Optional[Decimal]:
    if raw is None or not str(raw).strip():
        return None
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _r_initial(leverage: Decimal) -> Optional[Decimal]:
    if leverage <= 1:
        return None
    return Decimal(1) / (leverage - 1)


async def seed_credit_risk_configs() -> list[str]:
    """按 F7/F6 派生统一信贷配置（只补缺失行，幂等；返回本次写入的 key）。

    - ``credit_leverage = loan_leverage_k + 1``：与旧 ``compute_max_borrow`` 精确等价，
      **不放松授信**。旧 ``k + 1 > 20``（即 k > 19）时拒绝写入并打 CRITICAL。
    - ``credit_maintenance_ratio``：迁移期沿用有效的 ``liquidation_hard_threshold``
      （要求 ``0 < hard < R_initial(credit_leverage)``）；无效则不写，等 20x 启用前
      由运营显式设为 0.04（F6）。
    - 每次写入都补 ``config_set`` 审计，``source="credit_migration"``。
    """
    from app.models.base import SiteConfig
    from app.services import audit_service

    keys = (CREDIT_LEVERAGE_KEY, CREDIT_MAINTENANCE_KEY, LEGACY_LEVERAGE_KEY, LEGACY_HARD_THRESHOLD_KEY)
    seeded: list[str] = []
    async with async_session_maker() as session:
        async with session.begin():
            rows = {
                r.key: r
                for r in (await session.execute(
                    select(SiteConfig).where(SiteConfig.key.in_(keys))
                )).scalars().all()
            }

            def _value(key: str) -> Optional[str]:
                row = rows.get(key)
                return None if row is None else row.value

            leverage = _decimal_or_none(_value(CREDIT_LEVERAGE_KEY))
            if leverage is None:
                legacy_k = _decimal_or_none(_value(LEGACY_LEVERAGE_KEY))
                if legacy_k is None:
                    logger.warning(
                        "credit_leverage 未 seed：缺少 %s 且无 %s",
                        LEGACY_LEVERAGE_KEY, CREDIT_LEVERAGE_KEY,
                    )
                else:
                    candidate = legacy_k + Decimal(1)
                    if candidate > MAX_CREDIT_LEVERAGE:
                        logger.critical(
                            "拒绝 seed credit_leverage=%s：旧 %s=%s 映射后超过上限 %s（F7/F6）",
                            candidate, LEGACY_LEVERAGE_KEY, legacy_k, MAX_CREDIT_LEVERAGE,
                        )
                    elif candidate <= 1:
                        logger.critical(
                            "拒绝 seed credit_leverage=%s：旧 %s=%s 非法（必须 > 0）",
                            candidate, LEGACY_LEVERAGE_KEY, legacy_k,
                        )
                    else:
                        leverage = candidate
                        session.add(SiteConfig(
                            key=CREDIT_LEVERAGE_KEY,
                            value=format(candidate, "f"),
                            value_type="decimal",
                        ))
                        seeded.append(CREDIT_LEVERAGE_KEY)

            if _decimal_or_none(_value(CREDIT_MAINTENANCE_KEY)) is None:
                hard = _decimal_or_none(_value(LEGACY_HARD_THRESHOLD_KEY))
                r_initial = _r_initial(leverage) if leverage is not None else None
                if hard is None:
                    logger.warning(
                        "credit_maintenance_ratio 未 seed：缺少 %s；启用 20x 前必须显式设为 0.04",
                        LEGACY_HARD_THRESHOLD_KEY,
                    )
                elif r_initial is None:
                    logger.warning(
                        "credit_maintenance_ratio 未 seed：无法确定有效 credit_leverage"
                    )
                elif Decimal(0) < hard < r_initial:
                    session.add(SiteConfig(
                        key=CREDIT_MAINTENANCE_KEY,
                        value=format(hard, "f"),
                        value_type="decimal",
                    ))
                    seeded.append(CREDIT_MAINTENANCE_KEY)
                else:
                    logger.warning(
                        "credit_maintenance_ratio 未 seed：%s=%s 不满足 0 < hard < R_initial(%s)",
                        LEGACY_HARD_THRESHOLD_KEY, hard, r_initial,
                    )

            for key in seeded:
                row = (
                    await session.execute(select(SiteConfig).where(SiteConfig.key == key))
                ).scalars().one()
                audit_service.record(
                    session, "config_set",
                    payload={
                        "key": key,
                        "old": None,
                        "new": row.value,
                        "value_type": row.value_type,
                        "source": CREDIT_MIGRATION_SOURCE,
                    },
                )
    if seeded:
        logger.info("credit risk configs seeded: %s", ", ".join(seeded))
    return seeded
