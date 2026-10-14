"""Read-only operator projections; each current-state read uses one SQL snapshot."""
from datetime import timezone
from decimal import Decimal, InvalidOperation, localcontext
from fastapi import HTTPException
from sqlalchemy import select, func, case, true
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.fx import FxPair, FxTreasury, FxWallet, FxShortPosition
from app.models.audit import AuditEvent
from app.schemas.fx import (FxPairAdminDetail, FxAdminOverview, FxHoldingsSummary,
    FxLiquidityAmounts, FxLiquidityPreviewRequest, FxLiquidityPreview,
    FxLiquidityHistoryItem, FxLiquidityHistory, _MAX_GOLD)


def _prices(before, after):
    def price(value):
        if value is None or value.gold is None or value.foreign is None or value.gold <= 0 or value.foreign <= 0:
            return None
        return value.gold / value.foreign
    with localcontext() as ctx:
        ctx.prec = 48
        first, last = price(before), price(after)
        change = (last / first - 1) * 100 if first is not None and last is not None else None
    return first, last, change


def _state_query(db: AsyncSession, pair_id: int):
    # Column projections avoid stale ORM identity-map values after concurrent writes.
    timestamp = func.statement_timestamp() if db.get_bind().dialect.name == "postgresql" else func.current_timestamp()
    return select(*FxPair.__table__.c, FxTreasury.gold_balance,
                  FxTreasury.foreign_balance, timestamp.label("as_of"))\
        .outerjoin(FxTreasury, FxTreasury.pair_id == FxPair.id).where(FxPair.id == pair_id)


async def overview(db: AsyncSession, pair_id: int) -> FxAdminOverview:
    long = select(func.count(case((FxWallet.foreign_amount > 0, 1))).label("long_users"),
        func.coalesce(func.sum(FxWallet.foreign_amount), 0).label("long_foreign"),
        func.coalesce(func.sum(FxWallet.cost_basis), 0).label("long_cost_gold"))\
        .where(FxWallet.pair_id == pair_id).subquery()
    short = select(func.count(case(((FxShortPosition.principal_foreign + FxShortPosition.interest_foreign > 0), 1))).label("short_users"),
        func.coalesce(func.sum(FxShortPosition.principal_foreign), 0).label("short_principal_foreign"),
        func.coalesce(func.sum(FxShortPosition.interest_foreign), 0).label("short_recorded_interest_foreign"))\
        .where(FxShortPosition.pair_id == pair_id).subquery()
    row = (await db.execute(_state_query(db, pair_id).add_columns(*long.c, *short.c)
                            .join(long, true()).join(short, true()))).mappings().first()
    if row is None:
        raise HTTPException(404, "FX pair not found")
    data = dict(row)
    data["gold_balance"] = data["gold_balance"] or Decimal("0")
    data["foreign_balance"] = data["foreign_balance"] or Decimal("0")
    price, _, _ = _prices(FxLiquidityAmounts(gold=data["gold_reserve"], foreign=data["foreign_reserve"]), None)
    return FxAdminOverview(pair=FxPairAdminDetail(**data), reference_price=price,
        as_of=data["as_of"].replace(tzinfo=timezone.utc) if data["as_of"].tzinfo is None else data["as_of"], holdings=FxHoldingsSummary(**data))


async def liquidity_preview(db: AsyncSession, pair_id: int, req: FxLiquidityPreviewRequest) -> FxLiquidityPreview:
    row = (await db.execute(_state_query(db, pair_id))).mappings().first()
    if row is None:
        raise HTTPException(404, "FX pair not found")
    before = FxLiquidityAmounts(gold=row["gold_reserve"], foreign=row["foreign_reserve"])
    treasury = FxLiquidityAmounts(gold=row["gold_balance"] or Decimal("0"), foreign=row["foreign_balance"] or Decimal("0"))
    sign = 1 if req.action == "fund" else -1
    with localcontext() as ctx:
        ctx.prec = 48
        after = FxLiquidityAmounts(gold=before.gold + sign * req.gold_amount, foreign=before.foreign + sign * req.foreign_amount)
        treasury_after = FxLiquidityAmounts(gold=treasury.gold + sign * req.gold_amount, foreign=treasury.foreign + sign * req.foreign_amount)
    reason = None
    if row["archived"]:
        reason = "FX pair is archived"
    elif after.gold <= 0 or after.foreign <= 0:
        reason = "withdrawal would exhaust reserves"
    elif treasury_after.gold < 0 or treasury_after.foreign < 0 or (req.action == "withdraw" and row["gold_balance"] is None):
        reason = "withdrawal exceeds treasury balance"
    elif any(value > _MAX_GOLD for value in (after.gold, after.foreign, treasury_after.gold, treasury_after.foreign)):
        reason = "result exceeds storage range"
    first, last, change = _prices(before, after)
    return FxLiquidityPreview(pair_id=pair_id, **req.model_dump(), pool_version=row["pool_version"], as_of=row["as_of"].replace(tzinfo=timezone.utc) if row["as_of"].tzinfo is None else row["as_of"],
        pool_before=before, pool_after=after, treasury_before=treasury, treasury_after=treasury_after,
        price_before=first, price_after=last, price_change_pct=change, executable=reason is None, blocked_reason=reason)


def _audit_decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _audit_amounts(payload, name):
    value = payload.get(name)
    if not isinstance(value, dict):
        return None
    return FxLiquidityAmounts(gold=_audit_decimal(value.get("gold")), foreign=_audit_decimal(value.get("foreign")))


async def liquidity_history(db: AsyncSession, pair_id: int, before_id: int | None, limit: int) -> FxLiquidityHistory:
    stmt = select(AuditEvent).where(AuditEvent.ref_table == "fx_pair", AuditEvent.ref_id == pair_id,
        AuditEvent.event_type.in_(("fx_fund", "fx_withdraw")))
    if before_id is not None:
        stmt = stmt.where(AuditEvent.id < before_id)
    rows = (await db.execute(stmt.order_by(AuditEvent.id.desc()).limit(limit + 1))).scalars().all()
    items = []
    for row in rows[:limit]:
        payload = row.payload
        initial = payload.get("action") == "initial_issuance"
        before = None if initial else _audit_amounts(payload, "pool_before")
        after = _audit_amounts(payload, "pool_after")
        first, last, change = _prices(before, after)
        items.append(FxLiquidityHistoryItem(id=row.id, ts=row.ts, operator_user_id=row.operator_user_id,
            action="initial_issuance" if initial else ("fund" if row.event_type == "fx_fund" else "withdraw"),
            gold_amount=_audit_decimal(payload.get("gold_amount")), foreign_amount=_audit_decimal(payload.get("foreign_amount")),
            pool_before=before, pool_after=after, treasury_before=None if initial else _audit_amounts(payload, "treasury_before"),
            treasury_after=_audit_amounts(payload, "treasury_after"), price_before=first, price_after=last, price_change_pct=change))
    return FxLiquidityHistory(items=items, next_before_id=items[-1].id if len(rows) > limit else None)
