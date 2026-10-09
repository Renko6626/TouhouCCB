"""超管站点配置接口。"""
from __future__ import annotations
import logging
from decimal import Decimal, InvalidOperation
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.base import User, SiteConfig
from app.schemas.loan import SiteConfigItem, SiteConfigUpdate
from app.services import site_config, loan_sweep, liquidation_sweep
from app.services.credit.thresholds import MAX_LEVERAGE

router = APIRouter()
public_router = APIRouter()
logger = logging.getLogger("thccb.site_config")

_WHITELIST = {
    "homepage_fx_enabled": "bool",
    "loan_enabled": "bool",
    "loan_daily_rate": "decimal",
    "loan_sweep_interval_sec": "int",
    "liquidation_enabled": "bool",
    "liquidation_sweep_interval_sec": "int",
    "activity_mode_enabled": "bool",
    "quant_whitelist_user_ids": "string",
    "bot_detection_enabled": "bool",
    "bot_detection_interval_sec": "int",
    "bot_detection_window_sec": "int",
    "bot_freq_threshold": "int",
    "bot_late_night_threshold": "int",
    "bot_interval_stddev_ms_threshold": "int",
    "bot_fast_follow_trigger_cost": "decimal",
    "bot_fast_follow_latency_ms": "int",
    "bot_fast_follow_count_threshold": "int",
    "liquidation_partial_pct": "decimal",
    # 经济参数（admin 热配）
    "sell_fee_rate": "decimal",
    "initial_balance": "decimal",
    # ── 统一信贷风险（计划 §3.4）──
    "credit_new_risk_frozen": "bool",
    "credit_leverage": "decimal",
    "credit_maintenance_ratio": "decimal",
    "credit_risk_retry_limit": "int",
    # 全站开空总闸：默认 false（未 seed 时内核按 get_bool_or(..., False) 读取）。
    "fx_short_enabled": "bool",
}

#: 需要跨行交叉校验的 key（见 _validate_credit_update）。
_CREDIT_CROSS_CHECK_KEYS = frozenset({"credit_leverage", "credit_maintenance_ratio"})


@public_router.get("/homepage")
async def homepage_mode(db: AsyncSession = Depends(get_async_session)):
    # 只公开首页模式，直接读库以免切换受进程缓存影响。
    value = (await db.execute(select(SiteConfig.value).where(
        SiteConfig.key == "homepage_fx_enabled"))).scalar_one_or_none()
    enabled = value is None or value.strip().lower() == "true"
    return {"mode": "fx" if enabled else "prediction"}


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
        if key == "liquidation_sweep_interval_sec" and not (5 <= v <= 7200):
            raise HTTPException(status_code=400, detail="liquidation sweep 间隔必须在 [5, 7200] 秒")
        if key == "credit_risk_retry_limit" and not (1 <= v <= 10):
            raise HTTPException(status_code=400, detail="credit_risk_retry_limit 必须在 [1, 10]")
    elif t == "decimal":
        try:
            v = Decimal(value)
        except InvalidOperation:
            raise HTTPException(status_code=400, detail="decimal 解析失败")
        if not v.is_finite():
            raise HTTPException(status_code=400, detail="decimal 必须是有限数")
        if key == "loan_daily_rate" and not (Decimal("0") < v < Decimal("1")):
            raise HTTPException(status_code=400, detail="日利率必须在 (0, 1)")
        if key == "sell_fee_rate" and not (Decimal("0") <= v < Decimal("0.2")):
            raise HTTPException(status_code=400, detail="卖出手续费率必须在 [0, 0.2)")
        if key == "initial_balance" and not (Decimal("0") <= v <= Decimal("1000000")):
            raise HTTPException(status_code=400, detail="初始余额必须在 [0, 1000000]")
        if key == "credit_leverage" and not (Decimal("1") < v <= MAX_LEVERAGE):
            raise HTTPException(status_code=400, detail=f"credit_leverage 必须在 (1, {MAX_LEVERAGE}]")
        if key == "credit_maintenance_ratio" and not Decimal("0") < v:
            raise HTTPException(status_code=400, detail="credit_maintenance_ratio 必须 > 0")


async def _ensure_config_row(db: AsyncSession, key: str, default: str = "false") -> None:
    """Create a missing allowlisted key with its safe default on first explicit set.

    ``fx_short_enabled`` stays absent until an operator touches it (the kernels
    read it with ``get_bool_or(..., False)``, so unset means off).  The admin
    toggle must work without a dedicated migration seed, hence this narrow
    upsert of exactly the requested allowlisted boolean.
    """
    row = (await db.execute(
        select(SiteConfig).where(SiteConfig.key == key))).scalars().first()
    if row is None:
        db.add(SiteConfig(key=key, value=default, value_type=_WHITELIST[key]))
        await db.flush()


async def _validate_credit_update(db: AsyncSession, key: str, value: str) -> None:
    from app.services.credit.thresholds import validate_thresholds

    raw = await site_config.get_many(db, list(_CREDIT_CROSS_CHECK_KEYS))
    raw[key] = value
    try:
        validate_thresholds(raw["credit_leverage"], raw["credit_maintenance_ratio"])
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/site-config", response_model=List[SiteConfigItem])
async def list_configs(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    rows = await site_config.get_all(db)
    return [SiteConfigItem(
        key=r.key, value=r.value, value_type=r.value_type,
        updated_at=r.updated_at, updated_by=r.updated_by,
    ) for r in rows if r.key in _WHITELIST]


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
    if key in _CREDIT_CROSS_CHECK_KEYS:
        await _validate_credit_update(db, key, req.value)
    if key == "fx_short_enabled":
        await _ensure_config_row(db, key)

    try:
        row = await site_config.set_value(db, key, req.value, admin_user_id=admin.id)
    except site_config.SiteConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("SITECONFIG_SET admin_id=%s key=%s value=%s", admin.id, key, req.value)
    if key == "loan_sweep_interval_sec":
        try:
            await loan_sweep.reschedule(int(req.value))
        except Exception:
            logger.exception("reschedule failed")
    elif key == "liquidation_sweep_interval_sec":
        try:
            await liquidation_sweep.reschedule(int(req.value))
        except Exception:
            logger.exception("liquidation reschedule failed")

    return SiteConfigItem(
        key=row.key, value=row.value, value_type=row.value_type,
        updated_at=row.updated_at, updated_by=row.updated_by,
    )
