"""Superuser-only FX operations."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.base import User
from app.models.audit import AuditEvent
from app.models.fx import FxEvent, FxPair, FxTrade, FxTreasury
from app.schemas.fx import FxEventAdmin, FxPairAdmin, FxPairAdminDetail
from app.services import audit_service, site_config
from app.services.fx import scheduler

router = APIRouter()


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
                 before: dict, after: dict) -> None:
    db.add(AuditEvent(event_type=kind, operator_user_id=admin_id, ref_table=ref_table,
                      ref_id=ref_id, payload={"before": before, "after": after}))


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
    existing = (await db.execute(select(FxPair))).scalars().all()
    if req.status == "trading" and any(p.status == "trading" for p in existing):
        raise HTTPException(409, "only one trading FX pair is allowed")
    if req.target_min > req.target_price or req.target_price > req.target_max:
        raise HTTPException(422, "target price must be within target range")
    pair = FxPair(**req.model_dump())
    db.add(pair)
    await db.flush()
    db.add(FxTreasury(pair_id=pair.id, gold_balance=req.gold_reserve, foreign_balance=req.foreign_reserve))
    _admin_audit(db, "fx_pair_create", admin.id, "fx_pair", pair.id, {},
                 {"currency_code": pair.currency_code, "currency_name": pair.currency_name, "status": pair.status})
    audit_service.record(db, "fx_fund", operator_user_id=admin.id, ref_table="fx_pair", ref_id=pair.id,
                         payload={"action": "initial_issuance", "gold_amount": str(req.gold_reserve), "foreign_amount": str(req.foreign_reserve),
                                  "pool_after": {"gold": str(req.gold_reserve), "foreign": str(req.foreign_reserve)},
                                  "treasury_after": {"gold": str(req.gold_reserve), "foreign": str(req.foreign_reserve)}})
    await db.commit(); await db.refresh(pair)
    return pair


@router.patch("/pairs/{pair_id}", response_model=FxPairAdmin)
async def update_pair(pair_id: int, req: PairPatch, admin: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    pair = await db.get(FxPair, pair_id)
    if pair is None: raise HTTPException(404, "FX pair not found")
    if pair.status != "draft" and (req.currency_code is not None or req.currency_name is not None):
        raise HTTPException(409, "currency cannot be changed after opening")
    values = req.model_dump(exclude_unset=True)
    if values.get("status") == "trading":
        other = (await db.execute(select(FxPair).where(FxPair.id != pair_id, FxPair.status == "trading"))).scalars().first()
        if other: raise HTTPException(409, "only one trading FX pair is allowed")
    before = {key: str(getattr(pair, key)) for key in values}
    for key, value in values.items(): setattr(pair, key, value)
    if pair.target_min > pair.target_price or pair.target_price > pair.target_max: raise HTTPException(422, "target price must be within target range")
    pair.updated_at = datetime.now(timezone.utc)
    _admin_audit(db, "fx_pair_update", admin.id, "fx_pair", pair.id, before,
                 {key: str(value) for key, value in values.items()})
    await db.commit(); await db.refresh(pair)
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
    if req.key not in FX_CONFIG_KEYS: raise HTTPException(400, "unknown FX config key")
    try: row = await site_config.set_value(db, req.key, req.value, admin_user_id=admin.id)
    except Exception as exc: raise HTTPException(422, str(exc)) from exc
    return {row.key: row.value}


@router.get("/events", response_model=list[FxEventAdmin])
async def list_events(_: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    return (await db.execute(select(FxEvent).order_by(FxEvent.id.desc()))).scalars().all()


@router.post("/events", response_model=FxEventAdmin)
async def create_event(req: EventRequest, admin: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    pair = await db.get(FxPair, req.pair_id)
    if pair is None: raise HTTPException(404, "FX pair not found")
    cap = Decimal("0.20") if req.kind in {"black_swan", "black-swan", "black_swan_event"} else Decimal("0.05")
    if abs(req.shock_ratio) > cap: raise HTTPException(422, "shock ratio exceeds event kind cap")
    if not (Decimal("0.1") <= req.first_reaction_ratio <= Decimal("0.9")):
        raise HTTPException(422, "first reaction ratio must be between 0.1 and 0.9")
    event = FxEvent(**req.model_dump(exclude={"scheduled_at"}), operator_user_id=admin.id)
    db.add(event); await db.flush()
    _admin_audit(db, "fx_event_create", admin.id, "fx_event", event.id, {},
                 {"status": "draft", "pair_id": event.pair_id, "shock_ratio": str(event.shock_ratio),
                  "first_reaction_ratio": str(event.first_reaction_ratio), "budget": str(event.budget)})
    await db.commit(); await db.refresh(event)
    if req.scheduled_at is not None:
        scheduled = await scheduler.schedule_event(db, event.id, req.scheduled_at)
        _admin_audit(db, "fx_event_schedule", admin.id, "fx_event", event.id,
                     {"status": "draft"}, {"status": "scheduled", "scheduled_at": scheduled.scheduled_at.isoformat()})
        await db.commit()
        return scheduled
    return event


@router.post("/events/{event_id}/publish", response_model=FxEventAdmin)
async def publish_event(event_id: int, admin: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    return await scheduler.publish_event(db, event_id)


@router.post("/events/{event_id}/cancel", response_model=FxEventAdmin)
async def cancel_event(event_id: int, admin: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    event = (await db.execute(select(FxEvent).where(FxEvent.id == event_id))).scalars().first()
    if event is None: raise HTTPException(404, "FX event not found")
    if event.status not in {"draft", "scheduled"}: raise HTTPException(409, "published events cannot be cancelled")
    event.status = "cancelled"; event.operator_user_id = admin.id
    audit_service.record(db, "fx_event_cancel", operator_user_id=admin.id, ref_table="fx_event", ref_id=event.id, payload={"pair_id": event.pair_id})
    await db.commit(); await db.refresh(event)
    return event


@router.get("/pairs/{pair_id}/interventions", response_model=list[Intervention])
async def interventions(pair_id: int, _: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    return (await db.execute(select(FxTrade).where(FxTrade.pair_id == pair_id, FxTrade.source != "player").order_by(FxTrade.id.desc()))).scalars().all()
