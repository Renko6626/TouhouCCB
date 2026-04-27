"""兑换中心用户端 API。"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_async_session
from app.core.users import current_active_user
from app.models.base import User
from app.models.redemption import (
    RedemptionPartner, RedemptionBatch, RedemptionCode, BatchStatus,
)
from app.schemas.redemption import (
    PartnerPublic, BatchListItem, BatchDetailResponse,
    PurchaseRequest, PurchaseResponse,
    MyRedemptionItem, MyRedemptionDetail, MarkUsedRequest,
)
from app.services import redemption as svc

router = APIRouter()
logger = logging.getLogger("thccb.redemption")


@router.get("/batches", response_model=List[BatchListItem])
async def list_batches(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    rows = await svc.list_active_batches_with_stock(db)
    return [
        BatchListItem(
            id=b.id,
            partner=PartnerPublic(
                id=p.id, name=p.name, description=p.description,
                website_url=p.website_url, logo_url=p.logo_url,
            ),
            name=b.name,
            unit_price=b.unit_price,
            available_count=avail,
        )
        for b, p, avail in rows
    ]


@router.get("/batches/{batch_id}", response_model=BatchDetailResponse)
async def batch_detail(
    batch_id: int,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    b = await db.get(RedemptionBatch, batch_id)
    if b is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    p = await db.get(RedemptionPartner, b.partner_id)
    if p is None or not p.is_active or b.status != BatchStatus.ACTIVE:
        raise HTTPException(status_code=404, detail="批次不可用")
    avail = await svc.count_available_for_batch(db, b.id)
    return BatchDetailResponse(
        id=b.id,
        partner=PartnerPublic(
            id=p.id, name=p.name, description=p.description,
            website_url=p.website_url, logo_url=p.logo_url,
        ),
        name=b.name,
        unit_price=b.unit_price,
        available_count=avail,
        description=b.description,
    )


@router.post("/purchase", response_model=PurchaseResponse)
async def purchase(
    req: PurchaseRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        result = await svc.purchase_code(db, user_id=user.id, batch_id=req.batch_id)
    except svc.PurchaseError as e:
        await db.rollback()
        if e.code == "INSUFFICIENT_CASH":
            raise HTTPException(status_code=422, detail="资金不足")
        if e.code == "SOLD_OUT":
            raise HTTPException(status_code=409, detail="已售罄")
        if e.code == "BATCH_NOT_ACTIVE":
            raise HTTPException(status_code=409, detail="批次不可购买")
        if e.code == "BATCH_NOT_FOUND":
            raise HTTPException(status_code=404, detail="批次不存在")
        if e.code == "PER_USER_LIMIT_REACHED":
            raise HTTPException(status_code=429, detail="已达单用户单批次购买上限")
        raise
    await db.commit()
    logger.info(
        "REDEMPTION_PURCHASE user_id=%s batch_id=%s code_id=%s amount=%s",
        user.id, req.batch_id, result.code_id, result.paid_amount,
    )
    return PurchaseResponse(
        code_id=result.code_id,
        code_string=result.code_string,
        batch_name=result.batch_name,
        partner_name=result.partner_name,
        partner_website_url=result.partner_website_url,
        description=result.description,
        paid_amount=result.paid_amount,
        cash_after=result.cash_after,
    )


@router.get("/my", response_model=List[MyRedemptionItem])
async def my_redemptions(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    stmt = (
        select(RedemptionCode)
        .where(RedemptionCode.bought_by_user_id == user.id)
        .order_by(RedemptionCode.bought_at.desc())
    )
    codes = list((await db.execute(stmt)).scalars().all())
    out: List[MyRedemptionItem] = []
    for c in codes:
        b = await db.get(RedemptionBatch, c.batch_id)
        p = await db.get(RedemptionPartner, b.partner_id) if b else None
        out.append(MyRedemptionItem(
            code_id=c.id,
            batch_name=b.name if b else "",
            partner_name=p.name if p else "",
            partner_website_url=p.website_url if p else "",
            paid_amount=b.unit_price if b else 0,
            bought_at=c.bought_at,
            marked_used_by_user_at=c.marked_used_by_user_at,
        ))
    return out


@router.get("/my/{code_id}", response_model=MyRedemptionDetail)
async def my_redemption_detail(
    code_id: int,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    c = await db.get(RedemptionCode, code_id)
    if c is None or c.bought_by_user_id != user.id:
        raise HTTPException(status_code=404, detail="兑换记录不存在")
    b = await db.get(RedemptionBatch, c.batch_id)
    p = await db.get(RedemptionPartner, b.partner_id) if b else None
    return MyRedemptionDetail(
        code_id=c.id,
        code_string=c.code_string,
        batch_name=b.name if b else "",
        partner_name=p.name if p else "",
        partner_website_url=p.website_url if p else "",
        paid_amount=b.unit_price if b else 0,
        bought_at=c.bought_at,
        marked_used_by_user_at=c.marked_used_by_user_at,
        description=b.description if b else "",
    )


@router.post("/my/{code_id}/mark-used")
async def mark_used(
    code_id: int,
    req: MarkUsedRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    c = await db.get(RedemptionCode, code_id)
    if c is None or c.bought_by_user_id != user.id:
        raise HTTPException(status_code=404, detail="兑换记录不存在")
    c.marked_used_by_user_at = datetime.now(timezone.utc) if req.used else None
    db.add(c)
    await db.commit()
    return {"ok": True, "marked_used_by_user_at": c.marked_used_by_user_at}
