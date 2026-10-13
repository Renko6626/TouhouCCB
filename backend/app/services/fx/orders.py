"""Private trigger order storage; no funds are reserved here."""
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fx import FxPair, FxTriggerOrder
from app.schemas.fx import FxTriggerOrderCreate, FxTriggerOrderRead
from app.services.market_locks import lock_user


async def create_order(db: AsyncSession, user_id: int, req: FxTriggerOrderCreate) -> FxTriggerOrderRead:
    # Pair precedes User, matching trading/admin lock ordering.
    pair = (await db.execute(select(FxPair).where(FxPair.id == req.pair_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if pair is None or pair.archived or pair.status == 'draft':
        raise HTTPException(404, 'FX pair not found')
    if pair.status == 'closed':
        raise HTTPException(409, 'FX pair is closed')
    user = await lock_user(db, user_id)
    if not user.is_active:
        raise HTTPException(403, 'User is inactive')
    count = (await db.execute(select(func.count()).select_from(FxTriggerOrder).where(
        FxTriggerOrder.user_id == user_id, FxTriggerOrder.status == 'waiting'))).scalar_one()
    if count >= 3:
        raise HTTPException(409, '最多只能同时保留 3 张有效挂单')
    order = FxTriggerOrder(user_id=user_id, **req.model_dump())
    db.add(order)
    await db.flush()
    result = FxTriggerOrderRead.model_validate(order)
    await db.commit()
    return result


async def list_orders(db: AsyncSession, user_id: int, *, status: str | None = None,
                      before_id: int | None = None, limit: int = 50) -> list[FxTriggerOrderRead]:
    if status is not None and status not in {'waiting', 'succeeded', 'failed', 'cancelled'}:
        raise HTTPException(422, 'Invalid order status')
    if not 1 <= limit <= 100:
        raise HTTPException(422, 'limit must be within 1..100')
    stmt = select(FxTriggerOrder).where(FxTriggerOrder.user_id == user_id)
    if status is not None:
        stmt = stmt.where(FxTriggerOrder.status == status)
    if before_id is not None:
        stmt = stmt.where(FxTriggerOrder.id < before_id)
    rows = (await db.execute(stmt.order_by(FxTriggerOrder.id.desc()).limit(limit))).scalars().all()
    return [FxTriggerOrderRead.model_validate(row) for row in rows]


async def cancel_order(db: AsyncSession, user_id: int, order_id: int) -> FxTriggerOrderRead:
    await lock_user(db, user_id)
    order = (await db.execute(select(FxTriggerOrder).where(
        FxTriggerOrder.id == order_id, FxTriggerOrder.user_id == user_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if order is None:
        raise HTTPException(404, 'Order not found')
    if order.status not in {'waiting', 'cancelled'}:
        raise HTTPException(409, 'Order is already finished')
    if order.status == 'waiting':
        order.status = 'cancelled'
        order.finished_at = datetime.now(timezone.utc)
        await db.flush()
    result = FxTriggerOrderRead.model_validate(order)
    await db.commit()
    return result
