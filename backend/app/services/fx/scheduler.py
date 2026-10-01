"""FX event administration and five-second recovery scheduler.

统一信贷（WP6b，flag=unified_credit_enabled）：
- `fund_pair` / `withdraw_pair` 改变池子储备：单写守卫 + pair 独占门闩 +
  `pool_version += 1`（风险快照按 pool_version/储备版本失效）；
- `publish_event` 的首轮冲击是系统干预：pair 非 trading 或 reduce_only 时拒绝；
- 门闩在 pair 行锁之前获取，commit 后才释放；开关关闭时逐字段保持旧行为。
"""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_maker
from app.models.fx import FxEvent, FxEventStatus, FxPair, FxTrade, FxTreasury
from app.schemas.fx import FxEventAdmin, FxPairAdmin
from app.services import audit_service, site_config
from app.services.credit import flags as credit_flags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import OWNERSHIP
from app.services.fx.engine import FxEngine
from app.services.fx.amm import marginal_price

_scheduler: Optional[AsyncIOScheduler] = None
_JOB_ID = "fx-engine"
ENGINE = FxEngine()


def unified_credit_enabled() -> bool:
    if OWNERSHIP.reason is not None:
        OWNERSHIP.require_writes()
    return bool(credit_flags.get_flags().unified_credit_enabled)


def _utc(value: Optional[datetime] = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def schedule_event(db: AsyncSession, event_id: int, scheduled_at: datetime) -> FxEventAdmin:
    # Lock order is always fx_pair -> fx_event (engine.tick, publish_event,
    # schedule_event).  Two sessions that lock the pair and its event in
    # opposite order can deadlock on PostgreSQL, so the pair lock must be
    # acquired first.  The pair_id probe is a plain read; correctness is
    # guaranteed by the later locked re-read of both rows.
    pair_id = (await db.execute(
        select(FxEvent.pair_id).where(FxEvent.id == event_id)
    )).scalars().first()
    if pair_id is None:
        raise HTTPException(404, "FX event not found")
    pair = (await db.execute(
        select(FxPair).where(FxPair.id == pair_id).with_for_update().execution_options(populate_existing=True)
    )).scalars().first()
    if pair is None:
        raise HTTPException(404, "FX pair not found")
    if pair.archived:
        raise HTTPException(409, "FX pair is archived")
    event = (await db.execute(
        select(FxEvent).where(FxEvent.id == event_id).with_for_update().execution_options(populate_existing=True)
    )).scalars().first()
    if event is None:
        raise HTTPException(404, "FX event not found")
    if event.status not in {"draft", "scheduled"}:
        return FxEventAdmin.model_validate(event)
    planned = _utc(scheduled_at)
    end = planned + timedelta(seconds=int(event.window_sec or 600))
    rows = (await db.execute(select(FxEvent).where(
        FxEvent.pair_id == event.pair_id,
        FxEvent.id != event.id,
        FxEvent.status.in_(("scheduled", "published")),
    ))).scalars().all()
    for other in rows:
        other_start = _utc(other.scheduled_at if other.status == "scheduled" else other.published_at)
        other_end = other_start + timedelta(seconds=int(other.window_sec or 600))
        if planned < other_end and other_start < end:
            raise HTTPException(409, "scheduled event window overlaps existing event; choose a later UTC time")
    OWNERSHIP.require_writes()
    event.status, event.scheduled_at = "scheduled", planned
    await db.commit(); await db.refresh(event)
    return FxEventAdmin.model_validate(event)


async def publish_event(db: AsyncSession, event_id: int, now: Optional[datetime] = None) -> FxEventAdmin:
    now = _utc(now)
    unified = unified_credit_enabled()
    if unified:
        OWNERSHIP.require_writes()
    # 未加锁的 pair_id 探针只用于选择要锁的 pair 行（既有锁序：pair → event）。
    pair_id = (await db.execute(
        select(FxEvent.pair_id).where(FxEvent.id == event_id)
    )).scalars().first()
    if pair_id is None:
        raise HTTPException(404, "FX event not found")
    if not unified:
        admin, trade = await _publish_event_impl(db, event_id=event_id, pair_id=int(pair_id), now=now, unified=False)
    else:
        await db.commit()  # return the discovery connection before waiting on a gate
        # 首轮冲击改价 = 系统干预：pair 独占门闩必须在 DB 行锁之前。
        async with GATES.hold(exclusive=[GroupKey("fx", int(pair_id))]):
            admin, trade = await _publish_event_impl(db, event_id=event_id, pair_id=int(pair_id), now=now, unified=True)
    # Queue the committed first-reaction trade after the pair gate is released:
    # the bounded publisher never blocks, so publication cannot extend the gate
    # or fail the committed event transaction.
    if trade is not None:
        # Post-commit only (a replay/idempotent publish returns trade=None).
        # The hint is synchronous and cannot fail the committed event.
        try:
            from app.services.fx.trading import notify_market_data_committed
            notify_market_data_committed(int(trade.pair_id))
        except Exception:
            pass
        try:
            from app.services.fx.market_data import publish_trade
            await publish_trade(trade)
        except Exception:
            pass
    return admin


async def _publish_event_impl(
    db: AsyncSession, *, event_id: int, pair_id: int, now: datetime, unified: bool,
) -> tuple[FxEventAdmin, Optional[FxTrade]]:
    # Same lock order as schedule_event/engine.tick: fx_pair first, then fx_event.
    pair = (await db.execute(
        select(FxPair).where(FxPair.id == pair_id).with_for_update().execution_options(populate_existing=True)
    )).scalars().first()
    if pair is None:
        raise HTTPException(404, "FX pair not found")
    event = (await db.execute(
        select(FxEvent).where(FxEvent.id == event_id).with_for_update().execution_options(populate_existing=True)
    )).scalars().first()
    if event is None:
        raise HTTPException(404, "FX event not found")
    if event.status == "published" or event.status == "completed":
        return FxEventAdmin.model_validate(event), None
    if not await site_config.get_bool_or(db, "fx_enabled", False):
        raise HTTPException(403, "FX trading is disabled")
    if pair.archived:
        raise HTTPException(409, "FX pair is archived")
    if unified and (str(pair.status).strip().lower() != "trading" or bool(pair.reduce_only)):
        # F9：halted（paused 且未显式 reduce_only）与 reduce_only 都不允许系统干预
        raise HTTPException(409, "pair is halted or reduce-only: system intervention disabled")
    if event.status in {"cancelled"}:
        raise HTTPException(409, "event is cancelled")
    conflict = (await db.execute(select(FxEvent).where(
        FxEvent.pair_id == event.pair_id, FxEvent.status == "published", FxEvent.id != event.id
    ))).scalars().first()
    if conflict is not None: raise HTTPException(409, "another FX event is active for this pair")
    budget = Decimal(event.budget or 0)
    if budget <= 0 or Decimal(event.first_reaction_ratio or 0) == 0:
        raise HTTPException(422, "event needs a non-zero first reaction budget")
    window = int(event.window_sec or 600)
    event.window_sec = window
    if window < 30 or window > 1800:
        raise HTTPException(422, "event window must be between 30 and 1800 seconds")
    shock = Decimal(event.shock_ratio or 0)
    first_ratio = Decimal(event.first_reaction_ratio or 0)
    cap = Decimal("0.20") if str(event.kind).lower().replace("-", "_") in {"black_swan", "black_swan_event"} else Decimal("0.05")
    if abs(shock) > cap or not (Decimal("0.1") <= first_ratio <= Decimal("0.9")):
        raise HTTPException(422, "event parameters are outside allowed range")
    treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pair.id).with_for_update().execution_options(populate_existing=True))).scalars().first()
    OWNERSHIP.require_writes()
    if treasury is None: treasury = FxTreasury(pair_id=pair.id); db.add(treasury); await db.flush()
    if treasury.spend_date != now.date(): treasury.spend_date, treasury.daily_spend = now.date(), Decimal("0")
    snapshot_spent = Decimal(str((event.parameter_snapshot or {}).get("spent", "0")))
    if snapshot_spent >= budget: raise HTTPException(409, "event budget exhausted")
    target_before = Decimal(pair.target_price)
    reference = Decimal(getattr(pair, "initial_price", None) or target_before)
    lower = max(Decimal(pair.target_min), reference * Decimal("0.5"))
    upper = min(Decimal(pair.target_max), reference * Decimal("2"))
    new_target = max(lower, min(upper, target_before * (Decimal("1") + shock)))
    current_price = marginal_price(pair.gold_reserve, pair.foreign_reserve)
    desired_price = current_price * ((new_target / current_price).ln() * first_ratio).exp()
    OWNERSHIP.require_writes()
    event.parameter_snapshot = {
        "shock_ratio": str(event.shock_ratio or 0), "first_reaction_ratio": str(event.first_reaction_ratio or 0),
        "window_sec": event.window_sec, "budget": str(event.budget or 0),
        "target_before": str(target_before), "published_at": now.isoformat(),
        "target_after": str(new_target),
    }
    event.status, event.published_at = "published", now
    # The first reaction is represented by the same zero-fee system AMM path as later ticks.
    moved = await ENGINE._safe_system_move(
        db, pair, treasury,
        desired_price,
        budget, await site_config.get_decimal_or(db, "fx_daily_budget", Decimal("100000")),
        source="event", now=now)
    if not moved:
        event.status = "cancelled"
        if not event.error_message:
            event.error_message = f"first reaction failed: {getattr(moved, 'reason', 'unknown')}"
        audit_service.record(db, "fx_event_cancel", ref_table="fx_event", ref_id=event.id,
                             operator_user_id=event.operator_user_id,
                             payload={"pair_id": pair.id, "reason": event.error_message})
        await db.commit()
        raise HTTPException(409, event.error_message)
    pair.target_price = new_target
    pair.updated_at = now
    OWNERSHIP.require_writes()
    event.parameter_snapshot = {
        **dict(event.parameter_snapshot or {}),
        "first_trade_id": moved.trade.id,
        "first_trade_amount": str(moved.trade.input_amount),
        "spent": str(moved.actual_spend),
    }
    audit_service.record(db, "fx_event_publish", ref_table="fx_event", ref_id=event.id,
                         operator_user_id=event.operator_user_id,
                         payload={"pair_id": pair.id, "target_before": str(event.parameter_snapshot["target_before"])})
    await db.commit(); await db.refresh(event)
    return FxEventAdmin.model_validate(event), moved.trade


async def fund_pair(db: AsyncSession, pair_id: int, gold_amount: Decimal, foreign_amount: Decimal,
                    operator_user_id: int):
    if not unified_credit_enabled():
        return await _fund_pair_impl(db, pair_id, gold_amount, foreign_amount, operator_user_id, unified=False)
    OWNERSHIP.require_writes()
    await db.commit()
    async with GATES.hold(exclusive=[GroupKey("fx", pair_id)]):
        return await _fund_pair_impl(db, pair_id, gold_amount, foreign_amount, operator_user_id, unified=True)


async def _fund_pair_impl(db: AsyncSession, pair_id: int, gold_amount: Decimal, foreign_amount: Decimal,
                          operator_user_id: int, unified: bool):
    pair = (await db.execute(select(FxPair).where(FxPair.id == pair_id).with_for_update().execution_options(populate_existing=True))).scalars().first()
    if pair is None: raise HTTPException(404, "FX pair not found")
    if pair.archived: raise HTTPException(409, "FX pair is archived")
    gold_amount, foreign_amount = Decimal(gold_amount), Decimal(foreign_amount)
    if gold_amount < 0 or foreign_amount < 0 or (gold_amount == 0 and foreign_amount == 0): raise HTTPException(422, "fund amount must be positive")
    treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pair.id).with_for_update().execution_options(populate_existing=True))).scalars().first()
    OWNERSHIP.require_writes()
    if treasury is None:
        treasury = FxTreasury(pair_id=pair.id); db.add(treasury)
    before = {"pool_gold": str(pair.gold_reserve), "pool_foreign": str(pair.foreign_reserve),
              "treasury_gold": str(treasury.gold_balance), "treasury_foreign": str(treasury.foreign_balance)}
    OWNERSHIP.require_writes()
    pair.gold_reserve += gold_amount; pair.foreign_reserve += foreign_amount; pair.updated_at = _utc()
    treasury.gold_balance += gold_amount; treasury.foreign_balance += foreign_amount; treasury.updated_at = _utc()
    if unified:
        pair.pool_version += 1
    audit_service.record(db, "fx_fund", operator_user_id=operator_user_id, ref_table="fx_pair", ref_id=pair.id,
                         payload={"gold_amount": str(gold_amount), "foreign_amount": str(foreign_amount),
                                  "pool_before": {"gold": before["pool_gold"], "foreign": before["pool_foreign"]},
                                  "treasury_before": {"gold": before["treasury_gold"], "foreign": before["treasury_foreign"]},
                                  "pool_after": {"gold": str(pair.gold_reserve), "foreign": str(pair.foreign_reserve)},
                                  "treasury_after": {"gold": str(treasury.gold_balance), "foreign": str(treasury.foreign_balance)}})
    await db.commit(); await db.refresh(pair)
    return FxPairAdmin.model_validate(pair)


async def withdraw_pair(db: AsyncSession, pair_id: int, gold_amount: Decimal, foreign_amount: Decimal,
                        operator_user_id: int):
    if not unified_credit_enabled():
        return await _withdraw_pair_impl(db, pair_id, gold_amount, foreign_amount, operator_user_id, unified=False)
    OWNERSHIP.require_writes()
    await db.commit()
    async with GATES.hold(exclusive=[GroupKey("fx", pair_id)]):
        return await _withdraw_pair_impl(db, pair_id, gold_amount, foreign_amount, operator_user_id, unified=True)


async def _withdraw_pair_impl(db: AsyncSession, pair_id: int, gold_amount: Decimal, foreign_amount: Decimal,
                              operator_user_id: int, unified: bool):
    pair = (await db.execute(select(FxPair).where(FxPair.id == pair_id).with_for_update().execution_options(populate_existing=True))).scalars().first()
    if pair is None: raise HTTPException(404, "FX pair not found")
    if pair.archived: raise HTTPException(409, "FX pair is archived")
    gold_amount, foreign_amount = Decimal(gold_amount), Decimal(foreign_amount)
    if gold_amount < 0 or foreign_amount < 0 or pair.gold_reserve - gold_amount <= 0 or pair.foreign_reserve - foreign_amount <= 0:
        raise HTTPException(409, "withdrawal would exhaust reserves")
    treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pair.id).with_for_update().execution_options(populate_existing=True))).scalars().first()
    if treasury is None or treasury.gold_balance < gold_amount or treasury.foreign_balance < foreign_amount:
        raise HTTPException(409, "withdrawal exceeds treasury balance")
    before = {"pool_gold": str(pair.gold_reserve), "pool_foreign": str(pair.foreign_reserve),
              "treasury_gold": str(treasury.gold_balance), "treasury_foreign": str(treasury.foreign_balance)}
    OWNERSHIP.require_writes()
    pair.gold_reserve -= gold_amount; pair.foreign_reserve -= foreign_amount; pair.updated_at = _utc()
    treasury.gold_balance -= gold_amount; treasury.foreign_balance -= foreign_amount; treasury.updated_at = _utc()
    if unified:
        pair.pool_version += 1
    audit_service.record(db, "fx_withdraw", operator_user_id=operator_user_id, ref_table="fx_pair", ref_id=pair.id,
                         payload={"gold_amount": str(gold_amount), "foreign_amount": str(foreign_amount),
                                  "pool_before": {"gold": before["pool_gold"], "foreign": before["pool_foreign"]},
                                  "treasury_before": {"gold": before["treasury_gold"], "foreign": before["treasury_foreign"]},
                                  "pool_after": {"gold": str(pair.gold_reserve), "foreign": str(pair.foreign_reserve)},
                                  "treasury_after": {"gold": str(treasury.gold_balance), "foreign": str(treasury.foreign_balance)}})
    await db.commit(); await db.refresh(pair)
    return FxPairAdmin.model_validate(pair)


async def _tick_safe() -> None:
    try:
        async with async_session_maker() as db:
            due = (await db.execute(select(FxEvent.id).where(FxEvent.status == "scheduled", FxEvent.scheduled_at <= _utc()).order_by(FxEvent.scheduled_at.asc()))).scalars().all()
            if await site_config.get_bool_or(db, "fx_enabled", False):
                for event_id in due:
                    try: await publish_event(db, event_id)
                    except HTTPException:
                        # A rejected event may still hold pair/event row locks.
                        # Release them before acquiring another product gate.
                        await db.rollback()
        await ENGINE.tick()
    except Exception:
        import logging; logging.getLogger(__name__).exception("fx scheduler tick failed")


async def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None: return
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(_tick_safe, "interval", seconds=5, id=_JOB_ID, max_instances=1)
    _scheduler.start()


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False); _scheduler = None
