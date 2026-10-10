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
    """Unsafe credit configuration: refuse startup."""

#: site_config keys（计划 §3.4 冻结，后续 WP 不得改名）
KEY_CREDIT_NEW_RISK_FROZEN = "credit_new_risk_frozen"
KEY_CREDIT_LEVERAGE = "credit_leverage"
KEY_CREDIT_MAINTENANCE_RATIO = "credit_maintenance_ratio"
KEY_CREDIT_RISK_RETRY_LIMIT = "credit_risk_retry_limit"

#: 只读实例声明；只读实例必须显式关闭全部写调度器（spec §6.1）。
READ_ONLY_ENV = "THCCB_READ_ONLY_INSTANCE"

DEFAULT_RETRY_LIMIT = 3
DEFAULT_CREDIT_LEVERAGE = Decimal("10")
DEFAULT_CREDIT_MAINTENANCE_RATIO = Decimal("0.04")

FLAG_KEYS = (
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

    credit_new_risk_frozen: bool = False
    credit_leverage: Optional[Decimal] = DEFAULT_CREDIT_LEVERAGE
    credit_maintenance_ratio: Optional[Decimal] = DEFAULT_CREDIT_MAINTENANCE_RATIO
    credit_risk_retry_limit: int = DEFAULT_RETRY_LIMIT
    read_only_instance: bool = False
    @property
    def thresholds(self) -> Optional[RiskThresholds]:
        if (self.credit_leverage is None
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
    leverage = _parse_decimal(raw.get(KEY_CREDIT_LEVERAGE))
    maintenance = _parse_decimal(raw.get(KEY_CREDIT_MAINTENANCE_RATIO))
    if leverage is None or maintenance is None:
        raise CreditConfigError("missing or invalid credit thresholds")
    try:
        derive_thresholds(leverage, maintenance)
    except ValueError as exc:
        raise CreditConfigError(str(exc)) from exc
    return CreditFlags(
        credit_new_risk_frozen=_parse_bool(raw.get(KEY_CREDIT_NEW_RISK_FROZEN), default=False),
        credit_leverage=leverage, credit_maintenance_ratio=maintenance,
        credit_risk_retry_limit=_parse_int(raw.get(KEY_CREDIT_RISK_RETRY_LIMIT), default=DEFAULT_RETRY_LIMIT),
        read_only_instance=read_only,
    )


#: 进程级缓存：默认参数用于直接构造；启动仍须加载持久配置。
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
    """重置为有效默认参数（测试 fixture 用）。"""
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
    """Every instance requires valid persisted thresholds before publishing its cache."""
    from app.services import site_config as site_config_service
    raw = await site_config_service.get_many(session, list(FLAG_KEYS))
    flags = parse_flags(raw, read_only=read_only_from_env())
    set_flags(flags)
    logger.info("credit flags loaded: %s", flags)
    return flags
