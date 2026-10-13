"""FX liquidity funding and withdrawals, preserving accounting and pair gates."""
from __future__ import annotations
from app.services.trading_prices import notify_pricing_changed
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.fx.pool_events import append_pool_event
from app.services.fx import publisher
from app.services.fx.trading import notify_market_data_committed
from app.models.fx import FxPair, FxTreasury
from app.schemas.fx import FxPairAdmin
from app.services import audit_service
from app.services.credit import flags as credit_flags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import OWNERSHIP



def _utc(value: Optional[datetime] = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def fund_pair(db: AsyncSession, pair_id: int, gold_amount: Decimal, foreign_amount: Decimal,
                    operator_user_id: int):
    OWNERSHIP.require_writes()
    await db.commit()
    notify_pricing_changed(())
    async with GATES.hold(exclusive=[GroupKey("fx", pair_id)]):
        result = await _fund_pair_impl(db, pair_id, gold_amount, foreign_amount, operator_user_id)
    _publish_liquidity(result)
    return result


async def _fund_pair_impl(db: AsyncSession, pair_id: int, gold_amount: Decimal, foreign_amount: Decimal,
                          operator_user_id: int):
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
    pair.pool_version += 1
    audit = audit_service.record(db, "fx_fund", operator_user_id=operator_user_id, ref_table="fx_pair", ref_id=pair.id,
                         payload={"gold_amount": str(gold_amount), "foreign_amount": str(foreign_amount),
                                  "pool_before": {"gold": before["pool_gold"], "foreign": before["pool_foreign"]},
                                  "treasury_before": {"gold": before["treasury_gold"], "foreign": before["treasury_foreign"]},
                                  "pool_after": {"gold": str(pair.gold_reserve), "foreign": str(pair.foreign_reserve)},
                                  "treasury_after": {"gold": str(treasury.gold_balance), "foreign": str(treasury.foreign_balance)}})
    await db.flush()
    append_pool_event(db, pair_id=pair.id, kind="fund", created_at=pair.updated_at,
                      pre_reserves=(Decimal(before["pool_gold"]), Decimal(before["pool_foreign"])),
                      post_reserves=(pair.gold_reserve, pair.foreign_reserve),
                      gold_volume=Decimal("0"), audit_id=audit.id)
    await db.commit()
    notify_pricing_changed(())
    await db.refresh(pair)
    return FxPairAdmin.model_validate(pair)


async def withdraw_pair(db: AsyncSession, pair_id: int, gold_amount: Decimal, foreign_amount: Decimal,
                        operator_user_id: int):
    OWNERSHIP.require_writes()
    await db.commit()
    notify_pricing_changed(())
    async with GATES.hold(exclusive=[GroupKey("fx", pair_id)]):
        result = await _withdraw_pair_impl(db, pair_id, gold_amount, foreign_amount, operator_user_id)
    _publish_liquidity(result)
    return result


async def _withdraw_pair_impl(db: AsyncSession, pair_id: int, gold_amount: Decimal, foreign_amount: Decimal,
                              operator_user_id: int):
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
    pair.pool_version += 1
    audit = audit_service.record(db, "fx_withdraw", operator_user_id=operator_user_id, ref_table="fx_pair", ref_id=pair.id,
                         payload={"gold_amount": str(gold_amount), "foreign_amount": str(foreign_amount),
                                  "pool_before": {"gold": before["pool_gold"], "foreign": before["pool_foreign"]},
                                  "treasury_before": {"gold": before["treasury_gold"], "foreign": before["treasury_foreign"]},
                                  "pool_after": {"gold": str(pair.gold_reserve), "foreign": str(pair.foreign_reserve)},
                                  "treasury_after": {"gold": str(treasury.gold_balance), "foreign": str(treasury.foreign_balance)}})
    await db.flush()
    append_pool_event(db, pair_id=pair.id, kind="withdraw", created_at=pair.updated_at,
                      pre_reserves=(Decimal(before["pool_gold"]), Decimal(before["pool_foreign"])),
                      post_reserves=(pair.gold_reserve, pair.foreign_reserve),
                      gold_volume=Decimal("0"), audit_id=audit.id)
    await db.commit()
    notify_pricing_changed(())
    await db.refresh(pair)
    return FxPairAdmin.model_validate(pair)


def _publish_liquidity(pair: FxPairAdmin) -> None:
    """Wake the existing event consumer and publisher after commit and gate release."""
    notify_market_data_committed(pair.id)
    if pair.gold_reserve > 0 and pair.foreign_reserve > 0:
        publisher.enqueue_publication(pair.id, pair.gold_reserve / pair.foreign_reserve)
