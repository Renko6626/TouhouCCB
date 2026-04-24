"""SiteConfig 读写服务。值按 value_type 解析。"""
from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.base import SiteConfig


class SiteConfigError(Exception):
    pass


async def _fetch(session: AsyncSession, key: str) -> SiteConfig:
    result = await session.execute(select(SiteConfig).where(SiteConfig.key == key))
    row = result.scalars().first()
    if row is None:
        raise SiteConfigError(f"siteconfig key not found: {key}")
    return row


async def get_decimal(session: AsyncSession, key: str) -> Decimal:
    row = await _fetch(session, key)
    return Decimal(row.value)


async def get_int(session: AsyncSession, key: str) -> int:
    row = await _fetch(session, key)
    return int(row.value)


async def get_bool(session: AsyncSession, key: str) -> bool:
    row = await _fetch(session, key)
    return row.value.lower() in ("true", "1", "yes")


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
    return row
