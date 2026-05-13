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


async def get_int(session: AsyncSession, key: str) -> int:
    return int(await _get_raw(session, key))


async def get_bool(session: AsyncSession, key: str) -> bool:
    return (await _get_raw(session, key)).lower() in ("true", "1", "yes")


async def get_all(session: AsyncSession) -> list[SiteConfig]:
    result = await session.execute(select(SiteConfig).order_by(SiteConfig.key))
    return list(result.scalars().all())


async def set_value(
    session: AsyncSession,
    key: str,
    value: str,
    *,
    admin_user_id: Optional[int],
) -> SiteConfig:
    row = await _fetch(session, key)
    row.value = value
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by = admin_user_id
    session.add(row)
    await session.commit()
    await session.refresh(row)
    _cache.pop(key, None)  # 主动失效，让下次读取拿到新值
    return row
