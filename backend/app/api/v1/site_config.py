"""超管站点配置接口。"""
from __future__ import annotations
import logging
from decimal import Decimal, InvalidOperation
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.base import User
from app.schemas.loan import SiteConfigItem, SiteConfigUpdate
from app.services import site_config, loan_sweep

router = APIRouter()
logger = logging.getLogger("thccb.site_config")

_WHITELIST = {
    "loan_enabled": "bool",
    "loan_leverage_k": "decimal",
    "loan_daily_rate": "decimal",
    "loan_sweep_interval_sec": "int",
}


def _validate(key: str, value: str) -> None:
    t = _WHITELIST[key]
    if t == "bool":
        if value.lower() not in ("true", "false"):
            raise HTTPException(status_code=400, detail="bool 必须为 true/false")
    elif t == "int":
        try:
            v = int(value)
        except ValueError:
            raise HTTPException(status_code=400, detail="int 解析失败")
        if key == "loan_sweep_interval_sec" and not (10 <= v <= 3600):
            raise HTTPException(status_code=400, detail="sweep 间隔必须在 [10, 3600] 秒")
    elif t == "decimal":
        try:
            v = Decimal(value)
        except InvalidOperation:
            raise HTTPException(status_code=400, detail="decimal 解析失败")
        if key == "loan_daily_rate" and not (Decimal("0") < v < Decimal("1")):
            raise HTTPException(status_code=400, detail="日利率必须在 (0, 1)")
        if key == "loan_leverage_k" and not (Decimal("0") < v <= Decimal("10")):
            raise HTTPException(status_code=400, detail="杠杆倍数必须在 (0, 10]")


@router.get("/site-config", response_model=List[SiteConfigItem])
async def list_configs(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    rows = await site_config.get_all(db)
    return [SiteConfigItem(
        key=r.key, value=r.value, value_type=r.value_type,
        updated_at=r.updated_at, updated_by=r.updated_by,
    ) for r in rows]


@router.put("/site-config/{key}", response_model=SiteConfigItem)
async def update_config(
    key: str,
    req: SiteConfigUpdate,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    if key not in _WHITELIST:
        raise HTTPException(status_code=400, detail=f"未知配置 key：{key}")
    _validate(key, req.value)

    row = await site_config.set_value(db, key, req.value, admin_user_id=admin.id)
    logger.info("SITECONFIG_SET admin_id=%s key=%s value=%s", admin.id, key, req.value)

    if key == "loan_sweep_interval_sec":
        try:
            await loan_sweep.reschedule(int(req.value))
        except Exception:
            logger.exception("reschedule failed")

    return SiteConfigItem(
        key=row.key, value=row.value, value_type=row.value_type,
        updated_at=row.updated_at, updated_by=row.updated_by,
    )
