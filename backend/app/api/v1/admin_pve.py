"""PvE 机器人管理 endpoints（spec §7）。全部超管；挂 /api/v1/admin/pve（nginx /admin 限速带）。

资金操作（generate 初始注资 / fund 注资复活）走 ledger_service.record_entry，
与人工调账同一条审计链。决策日志读 ENGINE 内存环形缓冲（重启即弃，spec §2）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time as dtime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session, managed_transaction
from app.core.users import current_superuser
from app.models.base import Position, SiteConfig, Transaction, User
from app.models.bot import (
    BotProfile, BOT_STATUS_ACTIVE, BOT_STATUS_PAUSED, BOT_STATUS_RETIRED,
)
from app.services import audit_service, site_config
from app.services.pve import service as pve_service
from app.services.pve.attention import ACTIVE_PRESETS
from app.services.pve.client import PveTradeError
from app.services.pve.engine import ENGINE
from app.services.pve.templates import PARAM_DOCS, TEMPLATE_REGISTRY
from app.services.wealth import compute_users_holdings_value

router = APIRouter()
logger = logging.getLogger("thccb.admin_pve")

# ── 配置键注册表：value_type + 默认值 + 中文说明（引擎侧读取用 get_*_or 同默认，缺行不炸）──
PVE_CONFIG_SPEC: Dict[str, tuple[str, str, str]] = {
    "pve_enabled": ("bool", "false", "总闸：关掉后最迟一个 tick 内全体机器人停手"),
    # 改后需重启才生效（scheduler 启动时读）
    "pve_tick_interval_sec": ("int", "20", "引擎心跳间隔（秒）——改后需重启后端"),
    "pve_orders_per_min_cap": ("int", "30", "全体机器人每分钟下单总量上限"),
    "pve_single_order_cap_cny": ("decimal", "0", "单笔成交金额上限（¥）；0=不限（默认，让机器人也能砸盘）"),
    "pve_daily_turnover_cap_cny": ("decimal", "0", "单机器人当日成交额上限（¥，北京自然日）；0=不限（默认）"),
    "pve_max_slippage_bps": (
        "int", "2500",
        "滑点保护（基点）：报价偏离现价超过此值放弃下单——金额闸门放开后，这是约束"
        "单笔价格冲击的主护栏",
    ),
    "pve_death_floor_cny": ("decimal", "3", "死亡水位（¥）：现金+持仓清算价值低于此判死，注资可复活"),
    "pve_max_wakes_per_tick": ("int", "20", "每个 tick 最多唤醒的机器人数（削峰；实际放行数在其 1/2~1 倍间随机）"),
    "pve_activity_wave": (
        "decimal", "0.7",
        "全局活跃度潮汐幅度（0=关）：慢波动的市场情绪——活跃期全体看盘变勤、冷清期变懒，"
        "避免恒定速率的机器味",
    ),
    "pve_sentiment": (
        "string", "",
        '风向注入：{"tilts": {"<outcome_id>": 0.15}, "expires_at": "ISO 时间可省"}——'
        "给散户机器人吹一阵利好/利空（负数）风；清空=撤风",
    ),
    "leaderboard_include_bots": ("bool", "true", "排行榜是否计入机器人"),
    "wealth_stats_include_bots": ("bool", "true", "财富统计是否计入机器人"),
}


# ── Schemas ──────────────────────────────────────────────────────────────


class BotItem(BaseModel):
    profile_id: int
    user_id: int
    username: str
    template: str
    status: str
    params: dict
    market_scope: Optional[List[int]]
    cash: float
    holdings_value: float          # LCV 清算口径
    total_value: float
    today_turnover: float
    scheduled: bool                # 当前是否在引擎调度中
    next_action_at: Optional[str]
    last_trade_at: Optional[datetime]
    created_at: datetime


class GenerateItem(BaseModel):
    template: str
    count: int = Field(..., ge=1, le=50)


class GenerateRequest(BaseModel):
    items: List[GenerateItem] = Field(..., min_length=1)
    naming_style: str = Field("lowkey", pattern="^(npc|lowkey|phrase)$")
    initial_cash: Decimal = Field(..., ge=0, le=100000)
    market_scope: Optional[List[int]] = None


class PatchBotRequest(BaseModel):
    status: Optional[str] = Field(None, pattern="^(active|paused)$")
    template: Optional[str] = None
    params: Optional[dict] = None
    market_scope: Optional[List[int]] = None  # 显式传 null 清空范围（看 model_fields_set）
    # 改名：二选一——username 直接指定；rename_style 从 naming 词库重抽一个没被占用的
    username: Optional[str] = Field(None, min_length=2, max_length=32)
    rename_style: Optional[str] = Field(None, pattern="^(npc|lowkey|phrase)$")


class FundRequest(BaseModel):
    amount: Decimal = Field(..., gt=0, le=100000)
    reason: Optional[str] = Field(None, max_length=200)


# ── 总览 / 列表 ──────────────────────────────────────────────────────────


@router.get("/overview", summary="PvE 总览：引擎状态 + 编制统计")
async def pve_overview(
    db: AsyncSession = Depends(get_async_session),
    _: User = Depends(current_superuser),
):
    counts = {
        s: c
        for s, c in (
            await db.execute(select(BotProfile.status, func.count()).group_by(BotProfile.status))
        ).all()
    }
    enabled = await site_config.get_bool_or(db, "pve_enabled", False)
    return {
        "enabled": enabled,
        "counts": {"active": counts.get("active", 0), "paused": counts.get("paused", 0), "dead": counts.get("dead", 0)},
        "engine": ENGINE.snapshot(),
        "templates": sorted(TEMPLATE_REGISTRY.keys()),
        "active_presets": sorted(ACTIVE_PRESETS.keys()),
        "template_details": _template_details(),
        "param_docs": PARAM_DOCS,
    }


def _template_details() -> List[dict]:
    """模板图鉴：中文名 + docstring 解说 + 分组 + 默认参数（管理页渲染用）。
    active_preset=always 即量化型（与 spawn_params 的判定同一约定）。"""
    out = []
    for name, cls in sorted(TEMPLATE_REGISTRY.items()):
        doc = (cls.__doc__ or "").strip()
        out.append({
            "name": name,
            "title": cls.title or name,
            "summary": doc.splitlines()[0].strip() if doc else "",
            "description": doc,
            "group": "quant" if cls.default_params.get("active_preset") == "always" else "retail",
            "params": cls.default_params,
        })
    return out


def _today_start_utc() -> datetime:
    """北京自然日起点的 UTC 时刻（当日成交额口径与引擎 day_turnover 一致）。"""
    from app.services.pve.attention import TZ

    now_bj = datetime.now(TZ)
    return datetime.combine(now_bj.date(), dtime.min, tzinfo=TZ).astimezone(timezone.utc)


@router.get("/bots", response_model=List[BotItem], summary="机器人账户池列表")
async def list_bots(
    db: AsyncSession = Depends(get_async_session),
    _: User = Depends(current_superuser),
):
    rows = (
        await db.execute(
            select(BotProfile, User).join(User, User.id == BotProfile.user_id).order_by(BotProfile.id)
        )
    ).all()
    if not rows:
        return []
    uids = [u.id for _, u in rows]
    lcv = await compute_users_holdings_value(db, uids)
    turnover_rows = (
        await db.execute(
            select(Transaction.user_id, func.coalesce(func.sum(Transaction.gross), 0))
            .where(
                Transaction.user_id.in_(uids),
                Transaction.timestamp >= _today_start_utc(),
                Transaction.type.in_(("buy", "sell")),
            )
            .group_by(Transaction.user_id)
        )
    ).all()
    turnover = {uid: float(v) for uid, v in turnover_rows}
    items: List[BotItem] = []
    for profile, user in rows:
        rt = ENGINE.runtimes.get(profile.id)
        hv = float(lcv.get(user.id, Decimal("0")))
        items.append(
            BotItem(
                profile_id=profile.id,
                user_id=user.id,
                username=user.username,
                template=profile.template,
                status=profile.status,
                params=profile.params or {},
                market_scope=profile.market_scope,
                cash=float(user.cash),
                holdings_value=hv,
                total_value=float(user.cash) + hv,
                today_turnover=turnover.get(user.id, 0.0),
                scheduled=rt is not None,
                next_action_at=rt.next_action_at.isoformat(timespec="seconds") if rt else None,
                last_trade_at=profile.last_trade_at,
                created_at=profile.created_at,
            )
        )
    return items


# ── 编制管理 ─────────────────────────────────────────────────────────────


@router.post("/bots/generate", summary="批量生成机器人（初始注资走 ledger）")
async def generate_bots(
    payload: GenerateRequest,
    db: AsyncSession = Depends(get_async_session),
    admin: User = Depends(current_superuser),
):
    total = sum(it.count for it in payload.items)
    if total > 100:
        raise HTTPException(400, detail="单次最多生成 100 个")
    try:
        async with managed_transaction(db):
            created = await pve_service.generate_bots(
                db,
                items=[(it.template, it.count) for it in payload.items],
                naming_style=payload.naming_style,
                initial_cash=payload.initial_cash,
                market_scope=payload.market_scope,
                operator_user_id=admin.id,
            )
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    logger.info("pve_generate admin=%s count=%s", admin.id, len(created))
    return {"created": created}


@router.patch("/bots/{profile_id}", summary="个体干预：暂停/恢复、换模板、改参数、改市场范围")
async def patch_bot(
    profile_id: int,
    payload: PatchBotRequest,
    db: AsyncSession = Depends(get_async_session),
    admin: User = Depends(current_superuser),
):
    profile = (
        await db.execute(select(BotProfile).where(BotProfile.id == profile_id))
    ).scalars().first()
    if profile is None:
        raise HTTPException(404, detail="机器人不存在")
    if payload.template is not None and payload.template not in TEMPLATE_REGISTRY:
        raise HTTPException(400, detail=f"未知模板：{payload.template}")
    if payload.username is not None and payload.rename_style is not None:
        raise HTTPException(400, detail="username 与 rename_style 只能二选一")
    if profile.status == BOT_STATUS_RETIRED:
        raise HTTPException(409, detail="已退役的机器人不可再干预")
    changes: dict = {}
    async with managed_transaction(db):
        if payload.status is not None:
            changes["status"] = [profile.status, payload.status]
            profile.status = payload.status
        if payload.template is not None:
            changes["template"] = [profile.template, payload.template]
            profile.template = payload.template
        if payload.params is not None:
            changes["params"] = True
            profile.params = payload.params
        if "market_scope" in payload.model_fields_set:
            changes["market_scope"] = [profile.market_scope, payload.market_scope]
            profile.market_scope = payload.market_scope
        if payload.username is not None or payload.rename_style is not None:
            try:
                old, new = await pve_service.rename_bot(
                    db, profile=profile,
                    username=payload.username, style=payload.rename_style,
                )
            except pve_service.BotOpError as e:
                raise HTTPException(e.status_code, detail=e.detail)
            changes["username"] = [old, new]
    if changes:
        # 非资金操作不进 audit_event（资金动作已由 ledger 记账+审计）；运维日志留痕
        logger.info("pve_bot_patch admin=%s profile=%s changes=%s", admin.id, profile.id, changes)
    return {"ok": True, "changes": list(changes.keys())}


@router.delete("/bots/{profile_id}", summary="销毁：没交易过的真删，交易过的清算退休")
async def destroy_bot(
    profile_id: int,
    db: AsyncSession = Depends(get_async_session),
    admin: User = Depends(current_superuser),
):
    """双路径见 pve_service.destroy_bot 的 docstring。

    平仓走真实 sell（LMSR 份额正确退回、价格正确回落、留下 Transaction），
    且必须在 managed_transaction **之外**——每次 loopback 下单会开自己的事务，
    在外层事务里调用会与它争行锁。平不干净就 409 中止，不置 retired、不收现金；
    已卖出的部分保留（LMSR 成交不可回滚），重试会接着卖剩下的，是幂等的。
    """
    profile = (
        await db.execute(select(BotProfile).where(BotProfile.id == profile_id))
    ).scalars().first()
    if profile is None:
        raise HTTPException(404, detail="机器人不存在")
    if profile.status == BOT_STATUS_RETIRED:
        raise HTTPException(409, detail="该机器人已退役")
    # 下面的 expire_all() 会把 admin 也失效掉，先把要用的标量取出来
    admin_id, bot_user_id = admin.id, profile.user_id

    positions = (
        await db.execute(
            select(Position).where(Position.user_id == bot_user_id, Position.amount > 0)
        )
    ).scalars().all()
    sold, failed = [], []
    for pos in positions:
        try:
            # 强制平仓接受任意滑点——退役的仓位必须清干净，否则会留下无主份额
            await ENGINE.trader.sell(bot_user_id, pos.outcome_id, pos.amount, 10000)
            sold.append({"outcome_id": pos.outcome_id, "shares": float(pos.amount)})
        except PveTradeError as e:
            failed.append({"outcome_id": pos.outcome_id, "detail": e.detail})
        except Exception as e:  # noqa: BLE001 — 网络/回环异常照样要如实报给管理员
            failed.append({"outcome_id": pos.outcome_id, "detail": repr(e)})
    if failed:
        raise HTTPException(
            409,
            detail=f"平仓未完成，已卖出 {len(sold)} 个、失败 {len(failed)} 个："
                   f"{failed}。处理后重试销毁（幂等）",
        )

    # 上面的 loopback 下单在别的 session 改了 cash/positions；先失效再在事务里重查，
    # 不能对失效对象做属性懒加载（异步上下文里会 MissingGreenlet）
    db.expire_all()
    async with managed_transaction(db):
        profile = (
            await db.execute(select(BotProfile).where(BotProfile.id == profile_id))
        ).scalars().first()
        if profile is None:
            raise HTTPException(404, detail="机器人不存在")
        try:
            result = await pve_service.destroy_bot(
                db, profile=profile, operator_user_id=admin_id
            )
        except pve_service.BotOpError as e:
            raise HTTPException(e.status_code, detail=e.detail)
    result["sold"] = sold
    logger.warning(
        "pve_bot_destroy admin=%s profile=%s mode=%s username=%s sold=%s recovered=%s",
        admin_id, profile_id, result["mode"], result["username"], sold,
        result["recovered_cash"],
    )
    return result


@router.post("/bots/{profile_id}/fund", summary="注资（dead 顺带复活）")
async def fund_bot(
    profile_id: int,
    payload: FundRequest,
    db: AsyncSession = Depends(get_async_session),
    admin: User = Depends(current_superuser),
):
    profile = (
        await db.execute(select(BotProfile).where(BotProfile.id == profile_id))
    ).scalars().first()
    if profile is None:
        raise HTTPException(404, detail="机器人不存在")
    async with managed_transaction(db):
        user = await pve_service.fund_bot(
            db,
            profile=profile,
            amount=payload.amount,
            operator_user_id=admin.id,
            reason=payload.reason,
        )
        new_cash = float(user.cash)
        status = profile.status
    return {"ok": True, "new_cash": new_cash, "status": status}


@router.get("/bots/{profile_id}/log", summary="决策流水（内存环形缓冲，重启即弃）")
async def bot_log(
    profile_id: int,
    db: AsyncSession = Depends(get_async_session),
    _: User = Depends(current_superuser),
):
    profile = (
        await db.execute(select(BotProfile).where(BotProfile.id == profile_id))
    ).scalars().first()
    if profile is None:
        raise HTTPException(404, detail="机器人不存在")
    return {"profile_id": profile_id, "log": ENGINE.get_log(profile_id)}


# ── 全局配置 ─────────────────────────────────────────────────────────────


@router.get("/config", summary="PvE 全局配置（含未落库的默认值）")
async def get_config(
    db: AsyncSession = Depends(get_async_session),
    _: User = Depends(current_superuser),
):
    stored = await site_config.get_many(db, list(PVE_CONFIG_SPEC.keys()))
    return {
        key: {
            "value": stored.get(key, default),
            "value_type": vtype,
            "is_default": key not in stored,
            "label": label,
        }
        for key, (vtype, default, label) in PVE_CONFIG_SPEC.items()
    }


def _validate_config_value(key: str, vtype: str, value: str) -> str:
    if vtype == "bool":
        v = value.strip().lower()
        if v not in ("true", "false"):
            raise HTTPException(400, detail=f"{key} 需为 true/false")
        return v
    if vtype == "string":
        v = value.strip()
        if key == "pve_sentiment" and v:  # 空串=撤风，合法；非空必须是格式正确的风向 JSON
            try:
                data = json.loads(v)
                tilts = data["tilts"]
                if not isinstance(tilts, dict) or not tilts:
                    raise ValueError
                for oid, tilt in tilts.items():
                    int(oid), float(tilt)
                if data.get("expires_at"):
                    datetime.fromisoformat(data["expires_at"])
            except (ValueError, TypeError, KeyError):
                raise HTTPException(
                    400,
                    detail='pve_sentiment 格式：{"tilts": {"<outcome_id>": 0.15}, "expires_at": "ISO 时间可省"}',
                )
        return v
    try:
        if vtype == "int":
            int(value)
        else:
            Decimal(value)
    except (ValueError, InvalidOperation):
        raise HTTPException(400, detail=f"{key} 的值 {value!r} 不是合法 {vtype}")
    return value.strip()


@router.put("/config", summary="改 PvE 全局配置（缺行自动落库）")
async def put_config(
    payload: Dict[str, str],
    db: AsyncSession = Depends(get_async_session),
    admin: User = Depends(current_superuser),
):
    unknown = set(payload) - set(PVE_CONFIG_SPEC)
    if unknown:
        raise HTTPException(400, detail=f"未知配置键：{sorted(unknown)}")
    applied = {}
    for key, raw in payload.items():
        vtype, _default, _label = PVE_CONFIG_SPEC[key]
        value = _validate_config_value(key, vtype, raw)
        try:
            await site_config.set_value(db, key, value, admin_user_id=admin.id)
        except site_config.SiteConfigError:
            # 首次设置：行不存在，直接落库（set_value 只改已有行）
            db.add(SiteConfig(key=key, value=value, value_type=vtype, updated_by=admin.id))
            audit_service.record(
                db, "config_set",
                operator_user_id=admin.id,
                payload={"key": key, "old": None, "new": value, "value_type": vtype},
            )
            await db.commit()
            site_config.clear_cache()
        applied[key] = value
    return {"ok": True, "applied": applied}
