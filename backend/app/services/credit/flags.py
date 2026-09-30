"""统一信贷开关（计划 §3.4；进程级只读缓存 + 启动加载 + 测试覆写）。

设计要点：

- **只读缓存**：``load_flags()`` 在 FastAPI lifespan 启动时读一次 site_config，
  之后运行期不重读（``unified_credit_enabled`` 翻转需重启）。不引入锁。
- **默认全关**：未加载 / 未 seed 时 ``CreditFlags()`` 全为旧行为 —— 开关 false 时
  WP1 的落地对现有交易零行为变化。
- **拒绝带病启用（WP3 收紧）**：``unified_credit_enabled=true`` 但缺
  ``credit_maintenance_ratio``、``credit_leverage`` 非法（含 > 20）、或
  ``maintenance >= R_initial`` 时，``parse_flags`` 记 ``enable_requested=True``
  + ``disabled_reason``，且 ``unified_credit_enabled=False``（管理端 API 依赖该
  判定继续返回 400）。**启动时**（非只读实例）``load_flags`` 直接抛
  ``CreditConfigError`` 让进程起不来：绝不静默回落 legacy 强平——有 FX 抵押
  债务时旧强平处理不了。只读实例保持写禁用（ownership 冻结）后继续。
- **热生效冻结**：``credit_new_risk_frozen`` 是切换窗口的"停增险"闸，风险检查在
  运行期用 ``refresh_new_risk_frozen(session)`` 热读（绕过 site_config 的 60s TTL，
  保证同/跨进程翻转立即生效），再用同步 ``new_risk_frozen()`` 取值；
  ``unified_credit_enabled`` / 门槛 / 重试上限仍是重启生效。
- **测试覆写**：``set_flags()`` / ``clear_flags()`` / ``set_new_risk_frozen()``，
  无锁、无 IO。
- **只读实例钩子**：``write_schedulers_enabled()`` 供 main.py 与 WP3 ownership 消费。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional

from sqlalchemy import inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.credit.thresholds import RiskThresholds, derive_thresholds

logger = logging.getLogger("thccb.credit.flags")


class CreditConfigError(RuntimeError):
    """Unsafe credit capability/configuration: refuse startup instead of legacy fallback."""

#: site_config keys（计划 §3.4 冻结，后续 WP 不得改名）
KEY_UNIFIED_CREDIT_ENABLED = "unified_credit_enabled"
KEY_CREDIT_NEW_RISK_FROZEN = "credit_new_risk_frozen"
KEY_CREDIT_LEVERAGE = "credit_leverage"
KEY_CREDIT_MAINTENANCE_RATIO = "credit_maintenance_ratio"
KEY_CREDIT_RISK_RETRY_LIMIT = "credit_risk_retry_limit"

#: 只读实例声明；只读实例必须显式关闭全部写调度器（spec §6.1）。
READ_ONLY_ENV = "THCCB_READ_ONLY_INSTANCE"

DEFAULT_RETRY_LIMIT = 3

FLAG_KEYS = (
    KEY_UNIFIED_CREDIT_ENABLED,
    KEY_CREDIT_NEW_RISK_FROZEN,
    KEY_CREDIT_LEVERAGE,
    KEY_CREDIT_MAINTENANCE_RATIO,
    KEY_CREDIT_RISK_RETRY_LIMIT,
)

_TRUTHY = {"true", "1", "yes", "on"}


def _parse_bool(raw: Optional[str], *, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def _parse_decimal(raw: Optional[str]) -> Optional[Decimal]:
    if raw is None or not raw.strip():
        return None
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _parse_int(raw: Optional[str], *, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("credit flag credit_risk_retry_limit 非法，回落默认 %s: %r", default, raw)
        return default
    if not 1 <= value <= 10:
        logger.warning("credit flag credit_risk_retry_limit 必须在 [1, 10]，回落默认 %s: %r", default, raw)
        return default
    return value


def read_only_from_env(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return str(source.get(READ_ONLY_ENV, "")).strip().lower() in _TRUTHY


@dataclass(frozen=True)
class CreditFlags:
    """统一信贷进程级配置快照；``thresholds`` 为 None 表示风险引擎不可用。"""

    unified_credit_enabled: bool = False
    credit_new_risk_frozen: bool = False
    credit_leverage: Optional[Decimal] = None
    credit_maintenance_ratio: Optional[Decimal] = None
    credit_risk_retry_limit: int = DEFAULT_RETRY_LIMIT
    read_only_instance: bool = False
    #: 非 None 说明 ``unified_credit_enabled=true`` 被拒绝启用，值即原因。
    disabled_reason: Optional[str] = None
    #: 运营**要求**开启（原始值 true），即使因配置非法被拒也保留意图。
    #: ``load_flags`` 见到 ``enable_requested and disabled_reason`` 时非只读实例必须启动失败。
    enable_requested: bool = False

    @property
    def config_error(self) -> Optional[str]:
        """运营要求开启但配置非法 → 返回原因（否则 None）。"""
        return self.disabled_reason if self.enable_requested else None

    @property
    def thresholds(self) -> Optional[RiskThresholds]:
        if (not self.unified_credit_enabled or self.credit_leverage is None
                or self.credit_maintenance_ratio is None):
            return None
        try:
            return derive_thresholds(self.credit_leverage, self.credit_maintenance_ratio)
        except ValueError:
            return None

    @property
    def risk_engine_ready(self) -> bool:
        return self.thresholds is not None


def parse_flags(raw: Mapping[str, str], *, read_only: bool = False) -> CreditFlags:
    """从原始 site_config 值解析开关（纯函数，测试直接调用）。

    缺失 / 非法值一律回落安全默认：``unified_credit_enabled=False``；若原始值是
    true（运营要求开启）但配置非法，``enable_requested=True`` + ``disabled_reason``，
    启动路径据此拒绝启动（不回落到无法处理 FX 债务的 legacy 强平）。
    """
    enable_requested = _parse_bool(raw.get(KEY_UNIFIED_CREDIT_ENABLED), default=False)
    enabled = enable_requested
    frozen = _parse_bool(raw.get(KEY_CREDIT_NEW_RISK_FROZEN), default=False)
    leverage = _parse_decimal(raw.get(KEY_CREDIT_LEVERAGE))
    maintenance = _parse_decimal(raw.get(KEY_CREDIT_MAINTENANCE_RATIO))
    retry_limit = _parse_int(raw.get(KEY_CREDIT_RISK_RETRY_LIMIT), default=DEFAULT_RETRY_LIMIT)

    disabled_reason: Optional[str] = None
    if enabled:
        if leverage is None:
            disabled_reason = "missing_credit_leverage"
        elif maintenance is None:
            disabled_reason = "missing_credit_maintenance_ratio"
        else:
            try:
                derive_thresholds(leverage, maintenance)
            except ValueError as exc:
                disabled_reason = f"invalid_thresholds: {exc}"
        if disabled_reason is not None:
            enabled = False

    return CreditFlags(
        unified_credit_enabled=enabled,
        credit_new_risk_frozen=frozen,
        credit_leverage=leverage,
        credit_maintenance_ratio=maintenance,
        credit_risk_retry_limit=retry_limit,
        read_only_instance=read_only,
        disabled_reason=disabled_reason,
        enable_requested=enable_requested,
    )


#: 进程级缓存：未加载时全关（旧行为）。
_current: CreditFlags = CreditFlags()
#: 运行期热读的 credit_new_risk_frozen；None 表示沿用启动快照。
_hot_frozen: Optional[bool] = None


def get_flags() -> CreditFlags:
    return _current


def set_flags(flags: CreditFlags) -> None:
    """测试覆写（无锁）；同时清掉热读覆写，回到该快照的冻结值。"""
    global _current, _hot_frozen
    _current = flags
    _hot_frozen = None


def clear_flags() -> None:
    """重置为默认全关（测试 fixture 用）。"""
    global _current, _hot_frozen
    _current = CreditFlags()
    _hot_frozen = None


def new_risk_frozen() -> bool:
    """风险检查消费的当前"停增险"值：运行期热读值优先，否则启动快照。"""
    if _hot_frozen is not None:
        return _hot_frozen
    return _current.credit_new_risk_frozen


def set_new_risk_frozen(value: Optional[bool]) -> None:
    """测试/运维覆写热冻结值；``None`` 恢复沿用启动快照。"""
    global _hot_frozen
    _hot_frozen = None if value is None else bool(value)


async def refresh_new_risk_frozen(session: AsyncSession) -> bool:
    """运行期热读 ``credit_new_risk_frozen``（切换窗口闸门）。

    刻意绕过 ``site_config`` 的 60s TTL：冻结是运营应急闸，翻转必须立即生效。
    代价是每次风险检查多一条按主键的 SELECT（调用方只在真正要判定增险时调用）。
    """
    global _hot_frozen
    from app.models.base import SiteConfig

    raw = (await session.execute(
        select(SiteConfig.value).where(SiteConfig.key == KEY_CREDIT_NEW_RISK_FROZEN)
    )).scalars().first()
    _hot_frozen = _parse_bool(raw, default=False)
    return _hot_frozen


def write_schedulers_enabled() -> bool:
    """只读实例钩子：False 时调用方不得启动任何写调度器。

    实际判定 = 该钩子 and ``credit/ownership.py`` 的 ``writes_enabled``（main.py 组合）。
    """
    return not _current.read_only_instance


async def has_live_fx_short_obligation(session: AsyncSession) -> bool:
    """Probe persisted foreign debt/locked cash; real database errors propagate.

    Inspect first so a legitimately pre-short-migration database can boot to
    migrate without executing a SELECT against a table that does not exist.
    """
    from app.models.fx import FxShortPosition

    connection = await session.connection()
    exists = await connection.run_sync(
        lambda sync: inspect(sync).has_table(FxShortPosition.__tablename__)
    )
    if not exists:
        return False
    return (await session.execute(
        select(FxShortPosition.id).where(or_(
            FxShortPosition.principal_foreign > 0,
            FxShortPosition.interest_foreign > 0,
            FxShortPosition.restricted_gold > 0,
        )).limit(1)
    )).first() is not None


async def load_flags(session: AsyncSession) -> CreditFlags:
    """启动时读一次 site_config 并缓存；返回解析结果。

    Persisted foreign principal, interest or restricted gold requires unified
    credit even on read-only instances (gold-only risk reads are unsafe).
    Database errors propagate; the cache is published only after this check.

    ``enable_requested=true`` 但配置非法且无存量外币义务时：
    - 非只读实例 → 抛 ``CreditConfigError``，启动失败（不允许回落 legacy 强平）；
    - 只读实例 → CRITICAL 日志后继续（写已由 ownership 冻结）。
    """
    from app.services import site_config as site_config_service

    raw = await site_config_service.get_many(session, list(FLAG_KEYS))
    flags = parse_flags(raw, read_only=read_only_from_env())
    if not flags.unified_credit_enabled and await has_live_fx_short_obligation(session):
        raise CreditConfigError(
            "live fx_short obligation requires unified_credit_enabled=true"
        )
    if flags.config_error is not None:
        logger.critical(
            "unified_credit_enabled=true 但配置非法（%s）：拒绝降级到 legacy 强平"
            "（旧强平无法处理 FX 抵押债务）",
            flags.config_error,
        )
        set_flags(flags)
        if not flags.read_only_instance:
            raise CreditConfigError(flags.config_error)
        logger.critical("read-only instance: 保持经济写入禁用（ownership 已冻结）")
        return flags
    set_flags(flags)
    logger.info(
        "credit flags loaded: enabled=%s frozen=%s leverage=%s maintenance=%s retry=%s read_only=%s",
        flags.unified_credit_enabled,
        flags.credit_new_risk_frozen,
        flags.credit_leverage,
        flags.credit_maintenance_ratio,
        flags.credit_risk_retry_limit,
        flags.read_only_instance,
    )
    return flags
