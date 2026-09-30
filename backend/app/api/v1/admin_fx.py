"""Superuser-only FX operations."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.base import User
from app.models.audit import AuditEvent
from app.models.fx import FxEvent, FxPair, FxTrade, FxTreasury
from app.schemas.fx import FxEventAdmin, FxPairAdmin, FxPairAdminDetail
from app.services import audit_service, site_config
from app.services.fx import scheduler
from app.services.credit import flags as credit_flags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import OWNERSHIP

router = APIRouter()

# ── F8：同时 trading 的 FX pair 上限 ─────────────────────────────────────────
# 这只是**管理端运营限制**：数据模型、估值与执行接口都不写死品种数，
# 新增 pair 不需要改 schema（WP8a 验收：上限只在 admin 层、非 trading 数量不受限）。
MAX_TRADING_PAIRS = 3
# 计数检查必须与写入在同一个事务里串行，否则两个并发 PATCH/POST 会各自读到
# "还差一个"再一起提交，凑出第 4 个 trading pair：
# - PostgreSQL：事务级 advisory lock（多进程/多实例都串行），commit/rollback 自动释放；
# - 其他方言（SQLite 开发/测试）：本进程内 asyncio.Lock 覆盖"检查 → 写入 → commit"。
_TRADING_MUTATION_LOCK = asyncio.Lock()
_TRADING_CAPACITY_LOCK_KEY = 0x46585F5452414445  # "FX_TRADE" 的固定 64-bit key


def _require_writes():
    if (credit_flags.get_flags().unified_credit_enabled or OWNERSHIP.reason is not None
            or credit_flags.read_only_from_env()):
        OWNERSHIP.require_writes()


@asynccontextmanager
async def _pair_update_gate(db: AsyncSession, pair_id: int):
    _require_writes()
    if not credit_flags.get_flags().unified_credit_enabled:
        yield
        return
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("FX pair update requires a clean request session")
    await db.close()
    async with GATES.hold(exclusive=[GroupKey("fx", pair_id)]):
        _require_writes()
        yield


def _dialect_name(db: AsyncSession) -> str:
    """`db` 的方言名；测试替身（无 bind 的 session 适配器）返回空串。"""
    bind = None
    getter = getattr(db, "get_bind", None)
    if callable(getter):
        try:
            bind = getter()
        except Exception:  # pragma: no cover - 仅在异常 session 替身上触发
            bind = None
    if bind is None:
        bind = getattr(db, "bind", None)
    return getattr(getattr(bind, "dialect", None), "name", "")


async def _lock_trading_capacity(db: AsyncSession) -> None:
    """在计数前取事务级串行锁（PG）；非 PG 由调用方的 asyncio.Lock 兜底。"""
    if _dialect_name(db) == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _TRADING_CAPACITY_LOCK_KEY},
        )


async def _ensure_trading_capacity(db: AsyncSession, *, exclude_pair_id: Optional[int] = None) -> None:
    """当前事务内统计 trading pair 数，达到 F8 上限则 409（调用方持事务锁）。"""
    stmt = select(func.count()).select_from(FxPair).where(FxPair.status == "trading")
    if exclude_pair_id is not None:
        stmt = stmt.where(FxPair.id != exclude_pair_id)
    trading = int((await db.execute(stmt)).scalar_one())
    if trading >= MAX_TRADING_PAIRS:
        raise HTTPException(
            409,
            f"at most {MAX_TRADING_PAIRS} FX pairs may be trading at the same time",
        )


def _amount(v: Decimal, *, positive: bool = False) -> Decimal:
    if not v.is_finite() or (v <= 0 if positive else v < 0):
        raise ValueError("amount must be finite and positive" if positive else "amount must be finite and non-negative")
    if -v.as_tuple().exponent > 6:
        raise ValueError("amount must have at most 6 fractional digits")
    return v


class PairCreate(BaseModel):
    currency_code: str = Field(min_length=1, max_length=16, pattern=r"^[A-Z0-9_]+$")
    currency_name: str = Field(min_length=1, max_length=64)
    status: str = Field("draft", pattern="^(draft|trading|paused|closed)$")
    # F9：显式运营选项；默认 false = paused 仍是"全停"旧语义。
    reduce_only: bool = False
    gold_reserve: Decimal = Decimal("1")
    foreign_reserve: Decimal = Decimal("1")
    target_price: Decimal = Decimal("1")
    initial_price: Decimal = Decimal("1")
    target_min: Decimal = Decimal("0.5")
    target_max: Decimal = Decimal("2")
    buy_fee_rate: Decimal = Decimal("0")
    sell_fee_rate: Decimal = Decimal("0")

    @field_validator("gold_reserve", "foreign_reserve", "target_price", "initial_price", "target_min", "target_max")
    @classmethod
    def positive(cls, v): return _amount(v, positive=True)

    @field_validator("buy_fee_rate", "sell_fee_rate")
    @classmethod
    def fee(cls, v):
        if not v.is_finite() or not (Decimal("0") <= v < Decimal("1")):
            raise ValueError("fee rate must be between 0 (inclusive) and 1 (exclusive)")
        return v


class PairPatch(BaseModel):
    currency_code: Optional[str] = Field(None, min_length=1, max_length=16, pattern=r"^[A-Z0-9_]+$")
    currency_name: Optional[str] = Field(None, min_length=1, max_length=64)
    status: Optional[str] = Field(None, pattern="^(draft|trading|paused|closed)$")
    # F9：只有显式传 reduce_only 才改变只减仓语义（status 变更绝不隐式带它）。
    reduce_only: Optional[bool] = None
    target_price: Optional[Decimal] = None
    target_min: Optional[Decimal] = None
    target_max: Optional[Decimal] = None
    buy_fee_rate: Optional[Decimal] = None
    sell_fee_rate: Optional[Decimal] = None

    @field_validator("target_price", "target_min", "target_max")
    @classmethod
    def positive(cls, v): return None if v is None else _amount(v, positive=True)

    @field_validator("buy_fee_rate", "sell_fee_rate")
    @classmethod
    def fee(cls, v):
        if v is not None and (not v.is_finite() or not Decimal("0") <= v < Decimal("1")):
            raise ValueError("fee rate must be between 0 (inclusive) and 1 (exclusive)")
        return v


class FundRequest(BaseModel):
    gold_amount: Decimal = Decimal("0")
    foreign_amount: Decimal = Decimal("0")

    @field_validator("gold_amount", "foreign_amount")
    @classmethod
    def valid(cls, v): return _amount(v)

    @model_validator(mode="after")
    def at_least_one(self):
        if not self.gold_amount and not self.foreign_amount:
            raise ValueError("one currency amount must be positive")
        return self


class EventRequest(BaseModel):
    pair_id: int
    title: str = Field(min_length=1, max_length=200)
    body: str = Field("", max_length=5000)
    kind: str = Field("macro", min_length=1, max_length=32)
    shock_ratio: Decimal = Decimal("0")
    first_reaction_ratio: Decimal = Decimal("0.25")
    window_sec: int = Field(180, ge=30, le=1800)
    budget: Decimal = Field(gt=0)
    scheduled_at: Optional[datetime] = None

    @field_validator("shock_ratio", "first_reaction_ratio", "budget")
    @classmethod
    def finite_six(cls, v):
        if not v.is_finite() or -v.as_tuple().exponent > 6:
            raise ValueError("value must be finite with at most 6 fractional digits")
        return v


class ConfigUpdate(BaseModel):
    key: str
    value: str


class Intervention(BaseModel):
    id: int
    pair_id: int
    side: str
    input_amount: Decimal
    output_amount: Decimal
    post_price: Decimal
    source: str
    created_at: datetime


FX_CONFIG_KEYS = {k for k, _, _ in site_config.FX_DEFAULT_CONFIGS}


def _admin_audit(db: AsyncSession, kind: str, admin_id: int, ref_table: str, ref_id: int,
                 before: dict, after: dict, *, action: Optional[str] = None) -> None:
    payload: dict[str, Any] = {"before": before, "after": after}
    if action is not None:
        payload["action"] = action
    db.add(AuditEvent(event_type=kind, operator_user_id=admin_id, ref_table=ref_table,
                      ref_id=ref_id, payload=payload))


@router.get("/pairs", response_model=list[FxPairAdminDetail])
async def list_pairs(_: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    """Admin read model for all pairs (including drafts) plus treasury state.

    This is the read counterpart to create/fund/withdraw so the operator page
    can render pair targets and treasury budgets immediately after a refresh
    instead of relying on write responses.  Superuser only; never public.
    """
    rows = (await db.execute(
        select(FxPair, FxTreasury)
        .outerjoin(FxTreasury, FxTreasury.pair_id == FxPair.id)
        .order_by(FxPair.id)
    )).all()
    result: list[FxPairAdminDetail] = []
    for pair, treasury in rows:
        data = FxPairAdmin.model_validate(pair).model_dump()
        data.update({
            "gold_balance": treasury.gold_balance if treasury else Decimal("0"),
            "foreign_balance": treasury.foreign_balance if treasury else Decimal("0"),
            "daily_spend": treasury.daily_spend if treasury else Decimal("0"),
            "spend_date": treasury.spend_date if treasury else None,
        })
        result.append(FxPairAdminDetail(**data))
    return result


@router.post("/pairs", response_model=FxPairAdmin)
async def create_pair(req: PairCreate, admin: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    _require_writes()
    if req.target_min > req.target_price or req.target_price > req.target_max:
        raise HTTPException(422, "target price must be within target range")
    async with _TRADING_MUTATION_LOCK:
        # 计数与 INSERT 同事务同锁：并发创建不能各自读到"还差一个"再一起提交。
        await _lock_trading_capacity(db)
        if req.status == "trading":
            await _ensure_trading_capacity(db)
        _require_writes()
        pair = FxPair(**req.model_dump())
        db.add(pair)
        await db.flush()
        db.add(FxTreasury(pair_id=pair.id, gold_balance=req.gold_reserve, foreign_balance=req.foreign_reserve))
        _admin_audit(db, "fx_pair_create", admin.id, "fx_pair", pair.id, {},
                     {"currency_code": pair.currency_code, "currency_name": pair.currency_name,
                      "status": pair.status, "reduce_only": str(pair.reduce_only)})
        audit_service.record(db, "fx_fund", operator_user_id=admin.id, ref_table="fx_pair", ref_id=pair.id,
                             payload={"action": "initial_issuance", "gold_amount": str(req.gold_reserve), "foreign_amount": str(req.foreign_reserve),
                                      "pool_after": {"gold": str(req.gold_reserve), "foreign": str(req.foreign_reserve)},
                                      "treasury_after": {"gold": str(req.gold_reserve), "foreign": str(req.foreign_reserve)}})
        await db.commit()
    await db.refresh(pair)
    return pair


@router.patch("/pairs/{pair_id}", response_model=FxPairAdmin)
async def update_pair(pair_id: int, req: PairPatch, admin: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    pair = await db.get(FxPair, pair_id)
    if pair is None: raise HTTPException(404, "FX pair not found")
    if pair.status != "draft" and (req.currency_code is not None or req.currency_name is not None):
        raise HTTPException(409, "currency cannot be changed after opening")
    values = req.model_dump(exclude_unset=True)
    async with _pair_update_gate(db, pair_id), _TRADING_MUTATION_LOCK:
        await _lock_trading_capacity(db)
        # 锁内重读同一行（populate_existing 覆盖身份映射里的旧值）：并发 PATCH
        # 不能拿临界区之外读到的 stale status/reduce_only 做计数与审计 before。
        pair = (await db.execute(
            select(FxPair).where(FxPair.id == pair_id)
            .with_for_update().execution_options(populate_existing=True)
        )).scalars().first()
        if pair is None:
            raise HTTPException(404, "FX pair not found")
        if pair.status != "draft" and (req.currency_code is not None or req.currency_name is not None):
            raise HTTPException(409, "currency cannot be changed after opening")
        if "reduce_only" in values and values["reduce_only"] is None:
            raise HTTPException(422, "reduce_only must be a boolean")
        if values.get("status") == "trading":
            await _ensure_trading_capacity(db, exclude_pair_id=pair_id)
        _require_writes()
        before = {key: str(getattr(pair, key)) for key in values}
        for key, value in values.items(): setattr(pair, key, value)
        if credit_flags.get_flags().unified_credit_enabled:
            pair.pool_version += 1
        if pair.target_min > pair.target_price or pair.target_price > pair.target_max: raise HTTPException(422, "target price must be within target range")
        pair.updated_at = datetime.now(timezone.utc)
        # reduce_only 是显式运营开关：只在真正翻转时给审计打 action 标记，
        # status 变更（含切到 paused）绝不隐式改它。
        action = None
        if "reduce_only" in values and str(before["reduce_only"]) != str(pair.reduce_only):
            action = "reduce_only_toggle"
        _admin_audit(db, "fx_pair_update", admin.id, "fx_pair", pair.id, before,
                     {key: str(value) for key, value in values.items()}, action=action)
        await db.commit()
    await db.refresh(pair)
    return pair


@router.post("/pairs/{pair_id}/fund", response_model=FxPairAdmin)
async def fund_pair(pair_id: int, req: FundRequest, admin: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    return await scheduler.fund_pair(db, pair_id, req.gold_amount, req.foreign_amount, admin.id)


@router.post("/pairs/{pair_id}/withdraw", response_model=FxPairAdmin)
async def withdraw_pair(pair_id: int, req: FundRequest, admin: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    return await scheduler.withdraw_pair(db, pair_id, req.gold_amount, req.foreign_amount, admin.id)


@router.get("/config")
async def get_config(_: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    rows = await site_config.get_all(db)
    return {r.key: r.value for r in rows if r.key in FX_CONFIG_KEYS}


@router.put("/config")
async def put_config(req: ConfigUpdate, admin: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    _require_writes()
    if req.key not in FX_CONFIG_KEYS: raise HTTPException(400, "unknown FX config key")
    try: row = await site_config.set_value(db, req.key, req.value, admin_user_id=admin.id)
    except Exception as exc: raise HTTPException(422, str(exc)) from exc
    return {row.key: row.value}


@router.get("/events", response_model=list[FxEventAdmin])
async def list_events(_: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    return (await db.execute(select(FxEvent).order_by(FxEvent.id.desc()))).scalars().all()


@router.post("/events", response_model=FxEventAdmin)
async def create_event(req: EventRequest, admin: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    _require_writes()
    pair = await db.get(FxPair, req.pair_id)
    if pair is None: raise HTTPException(404, "FX pair not found")
    cap = Decimal("0.20") if req.kind in {"black_swan", "black-swan", "black_swan_event"} else Decimal("0.05")
    if abs(req.shock_ratio) > cap: raise HTTPException(422, "shock ratio exceeds event kind cap")
    if not (Decimal("0.1") <= req.first_reaction_ratio <= Decimal("0.9")):
        raise HTTPException(422, "first reaction ratio must be between 0.1 and 0.9")
    _require_writes()
    event = FxEvent(**req.model_dump(exclude={"scheduled_at"}), operator_user_id=admin.id)
    db.add(event); await db.flush()
    _admin_audit(db, "fx_event_create", admin.id, "fx_event", event.id, {},
                 {"status": "draft", "pair_id": event.pair_id, "shock_ratio": str(event.shock_ratio),
                  "first_reaction_ratio": str(event.first_reaction_ratio), "budget": str(event.budget)})
    _require_writes()
    await db.commit(); await db.refresh(event)
    if req.scheduled_at is not None:
        scheduled = await scheduler.schedule_event(db, event.id, req.scheduled_at)
        _admin_audit(db, "fx_event_schedule", admin.id, "fx_event", event.id,
                     {"status": "draft"}, {"status": "scheduled", "scheduled_at": scheduled.scheduled_at.isoformat()})
        _require_writes()
        await db.commit()
        return scheduled
    return event


@router.post("/events/{event_id}/publish", response_model=FxEventAdmin)
async def publish_event(event_id: int, admin: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    return await scheduler.publish_event(db, event_id)


@router.post("/events/{event_id}/cancel", response_model=FxEventAdmin)
async def cancel_event(event_id: int, admin: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    _require_writes()
    event = (await db.execute(select(FxEvent).where(FxEvent.id == event_id))).scalars().first()
    if event is None: raise HTTPException(404, "FX event not found")
    if event.status not in {"draft", "scheduled"}: raise HTTPException(409, "published events cannot be cancelled")
    _require_writes()
    event.status = "cancelled"; event.operator_user_id = admin.id
    audit_service.record(db, "fx_event_cancel", operator_user_id=admin.id, ref_table="fx_event", ref_id=event.id, payload={"pair_id": event.pair_id})
    _require_writes()
    await db.commit(); await db.refresh(event)
    return event


@router.get("/pairs/{pair_id}/interventions", response_model=list[Intervention])
async def interventions(
    pair_id: int,
    limit: int = Query(50, ge=1, le=200),
    source: str | None = Query(None, max_length=24),
    side: str | None = Query(None, pattern="^(buy|sell)$"),
    _: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    query = select(FxTrade).where(FxTrade.pair_id == pair_id, FxTrade.source != "player")
    if source:
        query = query.where(FxTrade.source == source)
    if side:
        query = query.where(FxTrade.side == side)
    return (await db.execute(query.order_by(FxTrade.id.desc()).limit(limit))).scalars().all()
