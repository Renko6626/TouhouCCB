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

router = APIRouter()
public_router = APIRouter()
logger = logging.getLogger("thccb.site_config")

_WHITELIST = {
    "homepage_fx_enabled": "bool",
    "loan_enabled": "bool",
    "loan_leverage_k": "decimal",
    "loan_daily_rate": "decimal",
    "loan_sweep_interval_sec": "int",
    "liquidation_enabled": "bool",
    "liquidation_sweep_interval_sec": "int",
    "liquidation_hard_threshold": "decimal",
    "liquidation_soft_threshold": "decimal",
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
    "liquidation_target_margin": "decimal",
    "liquidation_emergency_threshold": "decimal",
    # 经济参数（admin 热配）
    "sell_fee_rate": "decimal",
    "initial_balance": "decimal",
    # ── 统一信贷风险（计划 §3.4）──
    "unified_credit_enabled": "bool",
    "credit_new_risk_frozen": "bool",
    "credit_leverage": "decimal",
    "credit_maintenance_ratio": "decimal",
    "credit_risk_retry_limit": "int",
    # 全站开空总闸：默认 false（未 seed 时内核按 get_bool_or(..., False) 读取）。
    "fx_short_enabled": "bool",
}

#: 需要跨行交叉校验的 key（见 _validate_credit_update）。
_CREDIT_CROSS_CHECK_KEYS = frozenset({
    "unified_credit_enabled",
    "loan_leverage_k",
    "credit_leverage",
    "credit_maintenance_ratio",
})


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
        if key == "loan_leverage_k" and not (Decimal("0") < v <= Decimal("10")):
            raise HTTPException(status_code=400, detail="杠杆倍数必须在 (0, 10]")
        if key == "sell_fee_rate" and not (Decimal("0") <= v < Decimal("0.2")):
            raise HTTPException(status_code=400, detail="卖出手续费率必须在 [0, 0.2)")
        if key == "initial_balance" and not (Decimal("0") <= v <= Decimal("1000000")):
            raise HTTPException(status_code=400, detail="初始余额必须在 [0, 1000000]")
        if key == "credit_leverage" and not (Decimal("1") < v <= Decimal("20")):
            raise HTTPException(status_code=400, detail="credit_leverage 必须在 (1, 20]")
        if key == "credit_maintenance_ratio" and not (Decimal("0") < v < Decimal("1")):
            raise HTTPException(status_code=400, detail="credit_maintenance_ratio 必须在 (0, 1)")


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
    """统一信贷相关 key 的跨行校验（同步 _validate 拿不到其他行）。

    - ``unified_credit_enabled=true``：复用启动期同一套 parse_flags 校验，
      缺 maintenance / leverage 非法 / maintenance >= R_initial 一律拒绝启用。
    - 统一模式已开启时拒绝单独编辑旧 ``loan_leverage_k``（F7 单一杠杆口径）。
    - 改 credit_leverage / credit_maintenance_ratio 时，用另一个当前值交叉校验
      （两条门槛必须仍然满足 maintenance < R_initial）。
    """
    from app.services.credit import flags as credit_flags
    from app.services.credit.thresholds import validate_thresholds

    raw = await site_config.get_many(db, list(credit_flags.FLAG_KEYS))
    unified_on = raw.get(credit_flags.KEY_UNIFIED_CREDIT_ENABLED, "false").strip().lower() in (
        "true", "1", "yes", "on",
    )

    if key == credit_flags.KEY_UNIFIED_CREDIT_ENABLED:
        if value.strip().lower() in ("true", "1", "yes", "on"):
            probe = dict(raw)
            probe[key] = value
            parsed = credit_flags.parse_flags(probe)
            if not parsed.unified_credit_enabled:
                raise HTTPException(
                    status_code=400,
                    detail=f"拒绝启用 unified_credit_enabled：{parsed.disabled_reason}",
                )
        return

    if key == "loan_leverage_k" and unified_on:
        raise HTTPException(
            status_code=400,
            detail="统一信贷已启用：loan_leverage_k 不可再独立编辑（请改 credit_leverage）",
        )

    if key in (credit_flags.KEY_CREDIT_LEVERAGE, credit_flags.KEY_CREDIT_MAINTENANCE_RATIO):
        if key not in raw:
            # 迁移未派生（旧 k 非法 / hard 无效）时行不存在：给 400 而不是让 set_value 抛 500
            raise HTTPException(
                status_code=400,
                detail=f"{key} 尚未由迁移 seed；请先修正 loan_leverage_k / liquidation_hard_threshold 并重启后端",
            )
        other_key = (
            credit_flags.KEY_CREDIT_MAINTENANCE_RATIO
            if key == credit_flags.KEY_CREDIT_LEVERAGE
            else credit_flags.KEY_CREDIT_LEVERAGE
        )
        other_raw = raw.get(other_key)
        if other_raw is None or not str(other_raw).strip():
            return
        try:
            other = Decimal(str(other_raw))
        except InvalidOperation:
            return
        leverage = Decimal(value) if key == credit_flags.KEY_CREDIT_LEVERAGE else other
        maintenance = other if key == credit_flags.KEY_CREDIT_LEVERAGE else Decimal(value)
        try:
            validate_thresholds(leverage, maintenance)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))


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
    if key in _CREDIT_CROSS_CHECK_KEYS:
        await _validate_credit_update(db, key, req.value)
    if key == "fx_short_enabled":
        await _ensure_config_row(db, key)

    row = await site_config.set_value(db, key, req.value, admin_user_id=admin.id)
    logger.info("SITECONFIG_SET admin_id=%s key=%s value=%s", admin.id, key, req.value)
    if key == "unified_credit_enabled":
        # 翻转需重启进程：进程内 flag 缓存只在 lifespan 启动时加载（计划 §3.4）。
        logger.warning(
            "unified_credit_enabled=%s 已写入；进程内开关需重启后端才生效", req.value,
        )

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
