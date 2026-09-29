"""统一信贷开关（计划 §3.4；进程级只读缓存 + 启动加载 + 测试覆写）。

设计要点：

- **只读缓存**：``load_flags()`` 在 FastAPI lifespan 启动时读一次 site_config，
  之后运行期不重读（``unified_credit_enabled`` 翻转需重启）。不引入锁。
- **默认全关**：未加载 / 未 seed 时 ``CreditFlags()`` 全为旧行为 —— 开关 false 时
  WP1 的落地对现有交易零行为变化。
- **拒绝带病启用**：``unified_credit_enabled=true`` 但缺 ``credit_maintenance_ratio``、
  ``credit_leverage`` 非法（含 > 20）、或 ``maintenance >= R_initial`` 时，
  ``parse_flags`` 回落 ``unified_credit_enabled=False`` 并在 ``disabled_reason``
  记录原因；``load_flags`` 额外打 CRITICAL 日志。这样坏配置只会"没开"，不会用错门槛放贷。
- **测试覆写**：``set_flags()`` / ``clear_flags()``，无锁、无 IO。
- **只读实例钩子**：``write_schedulers_enabled()`` 供 main.py 与 WP3 ownership 消费；
  WP1 只提供钩子（读 ``THCCB_READ_ONLY_INSTANCE`` 环境变量），不改调度器语义。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.credit.thresholds import RiskThresholds, derive_thresholds

logger = logging.getLogger("thccb.credit.flags")

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
    #: 非 None 说明 ``unified_credit_enabled`` 被拒绝启用，值即原因。
    disabled_reason: Optional[str] = None

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

    缺失 / 非法值一律回落安全默认：``unified_credit_enabled=False``。
    """
    enabled = _parse_bool(raw.get(KEY_UNIFIED_CREDIT_ENABLED), default=False)
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
    )


#: 进程级缓存：未加载时全关（旧行为）。
_current: CreditFlags = CreditFlags()


def get_flags() -> CreditFlags:
    return _current


def set_flags(flags: CreditFlags) -> None:
    """测试覆写（无锁）。"""
    global _current
    _current = flags


def clear_flags() -> None:
    """重置为默认全关（测试 fixture 用）。"""
    global _current
    _current = CreditFlags()


def write_schedulers_enabled() -> bool:
    """只读实例钩子：False 时调用方不得启动任何写调度器。

    WP1 只暴露钩子；实际所有权判定由 WP3 ``credit/ownership.py`` 接管。
    """
    return not _current.read_only_instance


async def load_flags(session: AsyncSession) -> CreditFlags:
    """启动时读一次 site_config 并缓存；返回解析结果。"""
    from app.services import site_config as site_config_service

    raw = await site_config_service.get_many(session, list(FLAG_KEYS))
    flags = parse_flags(raw, read_only=read_only_from_env())
    if flags.disabled_reason is not None:
        logger.critical(
            "unified_credit_enabled=true 被拒绝启用（%s）；已回落 legacy 行为，"
            "修好配置后需重启进程",
            flags.disabled_reason,
        )
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
