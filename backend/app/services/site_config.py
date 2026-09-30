"""SiteConfig 读写服务。值按 value_type 解析。"""
from __future__ import annotations
import time
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.base import SiteConfig


class SiteConfigError(Exception):
    pass


# FX is deliberately disabled and has conservative defaults until an operator
# explicitly enables and funds a pair.  loan_migrate imports this list when it
# seeds a new or upgraded database.
FX_DEFAULT_CONFIGS = [
    ("fx_enabled", "false", "bool"),
    ("fx_short_enabled", "false", "bool"),
    ("fx_hourly_sigma", "0.002", "decimal"),
    ("fx_step_max_ratio", "0.001", "decimal"),
    ("fx_noise_interval_sec", "30", "int"),
    ("fx_noise_pool_ratio", "0.0001", "decimal"),
    ("fx_system_half_life_sec", "600", "int"),
    ("fx_default_price_move_limit", "0.005", "decimal"),
    ("fx_daily_budget", "100000", "decimal"),
]

FX_CONFIG_RULES = {
    "fx_enabled": lambda v: v.lower() in {"true", "false", "1", "0", "yes", "no"},
    "fx_short_enabled": lambda v: v.lower() in {"true", "false", "1", "0", "yes", "no"},
    "fx_hourly_sigma": lambda v: Decimal(v) >= 0 and Decimal(v) <= Decimal("1"),
    "fx_step_max_ratio": lambda v: Decimal(v) > 0 and Decimal(v) <= Decimal("1"),
    "fx_noise_interval_sec": lambda v: int(v) >= 1,
    "fx_noise_pool_ratio": lambda v: Decimal(v) > 0 and Decimal(v) <= Decimal("1"),
    "fx_system_half_life_sec": lambda v: int(v) >= 1,
    "fx_default_price_move_limit": lambda v: Decimal(v) > 0 and Decimal(v) <= Decimal("1"),
    "fx_daily_budget": lambda v: Decimal(v) >= 0,
}


# 进程级 TTL 缓存：key → (raw_value_str, expires_monotonic)
# 准静态配置（loan_daily_rate 等）在 set_value 时主动失效，其余 60s 自然过期。
_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 60.0


async def _fetch(session: AsyncSession, key: str) -> SiteConfig:
    result = await session.execute(select(SiteConfig).where(SiteConfig.key == key))
    row = result.scalars().first()
    if row is None:
        raise SiteConfigError(f"siteconfig key not found: {key}")
    return row


def clear_cache() -> None:
    """清空进程级缓存（测试 fixture 用；直接改 DB 绕过 set_value 时调用）。"""
    _cache.clear()


async def _get_raw(session: AsyncSession, key: str) -> str:
    now = time.monotonic()
    entry = _cache.get(key)
    if entry is not None and now < entry[1]:
        return entry[0]
    row = await _fetch(session, key)
    _cache[key] = (row.value, now + _CACHE_TTL)
    return row.value


async def get_decimal(session: AsyncSession, key: str) -> Decimal:
    return Decimal(await _get_raw(session, key))


async def get_decimal_or(session: AsyncSession, key: str, default: Decimal) -> Decimal:
    """读 decimal；key 不存在时返回 default（不抛 SiteConfigError）。

    用于「有硬编码安全默认」的可配置项（如 sell_fee_rate / initial_balance）：
    冷启动 / 未 seed 时回落默认，避免读配置抛错中断 hot path 或注册。
    """
    try:
        return await get_decimal(session, key)
    except SiteConfigError:
        return default


async def get_int(session: AsyncSession, key: str) -> int:
    return int(await _get_raw(session, key))


async def get_int_or(session: AsyncSession, key: str, default: int) -> int:
    """读 int；key 不存在时返回 default（不抛 SiteConfigError）。"""
    try:
        return await get_int(session, key)
    except SiteConfigError:
        return default


async def get_bool(session: AsyncSession, key: str) -> bool:
    return (await _get_raw(session, key)).lower() in ("true", "1", "yes")


async def get_bool_or(session: AsyncSession, key: str, default: bool) -> bool:
    """读 bool；key 不存在时返回 default（不抛 SiteConfigError）。"""
    try:
        return await get_bool(session, key)
    except SiteConfigError:
        return default


async def get_str(session: AsyncSession, key: str) -> str:
    return await _get_raw(session, key)


async def get_all(session: AsyncSession) -> list[SiteConfig]:
    result = await session.execute(select(SiteConfig).order_by(SiteConfig.key))
    return list(result.scalars().all())


async def get_many(session: AsyncSession, keys: list[str]) -> dict[str, str]:
    """批量取 raw value，省 N 次串行 query。

    流程：
    1. cache 命中的 key 直接取
    2. 未命中的所有 key 用一次 `WHERE key IN (...)` 拉
    3. 全部填回 cache

    返回 {key: raw_value_str}。**缺失的 key 不在返回 dict 里**（与单 key 版本
    抛 SiteConfigError 的行为不同——批量场景由调用方决定哪些 key 是 required）。
    调用方需要 .get(k) 或 .get(k, default) 取值。

    sweep / 批量决策场景用，避免阶段 A 4 次串行 query。
    """
    now = time.monotonic()
    result: dict[str, str] = {}
    missing: list[str] = []
    for key in keys:
        entry = _cache.get(key)
        if entry is not None and now < entry[1]:
            result[key] = entry[0]
        else:
            missing.append(key)
    if missing:
        rows = (await session.execute(
            select(SiteConfig).where(SiteConfig.key.in_(missing))
        )).scalars().all()
        for row in rows:
            _cache[row.key] = (row.value, now + _CACHE_TTL)
            result[row.key] = row.value
    return result


async def set_value(
    session: AsyncSession,
    key: str,
    value: str,
    *,
    admin_user_id: Optional[int],
) -> SiteConfig:
    _require_writes()
    row = await _fetch(session, key)
    _require_writes()
    if key in FX_CONFIG_RULES:
        try:
            if not FX_CONFIG_RULES[key](value):
                raise SiteConfigError(f"invalid FX config value: {key}")
        except (ValueError, ArithmeticError) as exc:
            raise SiteConfigError(f"invalid FX config value: {key}") from exc
    from app.services.credit import flags
    if (key in {"loan_daily_rate", "sell_fee_rate"} and flags.get_flags().unified_credit_enabled
            and Decimal(value) != Decimal(row.value)):
        # Runtime global fee changes also race newly-created market catalogs.
        # Configure before activation; maintenance must settle old-rate interest.
        raise SiteConfigError(f"统一信贷运行期间不能调整 {key}；须在停写维护中配置，日利率变更先结清旧率利息")
    old = row.value
    row.value = value
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by = admin_user_id
    session.add(row)
    from app.services import audit_service  # 局部 import 避免环
    audit_service.record(
        session, "config_set",
        operator_user_id=admin_user_id,
        payload={"key": key, "old": old, "new": value, "value_type": row.value_type},
    )
    _require_writes()
    await session.commit()
    await session.refresh(row)
    _cache.pop(key, None)  # 主动失效，让下次读取拿到新值
    return row


def _require_writes() -> None:
    from app.services.credit import flags
    from app.services.credit.ownership import OWNERSHIP
    if flags.get_flags().unified_credit_enabled or OWNERSHIP.reason is not None or flags.read_only_from_env():
        OWNERSHIP.require_writes()
