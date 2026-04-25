"""兑换中心管理员端 API。"""
from __future__ import annotations
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.base import User
from app.models.redemption import (
    RedemptionPartner, RedemptionBatch, BatchStatus,
)
from app.schemas.redemption import (
    PartnerCreate, PartnerUpdate, PartnerAdminItem,
    BatchCreate, BatchUpdate, BatchAdminItem,
    CsvImportRequest, CsvImportPreview,
    CsvImportConfirm, CsvImportResult,
)
from app.services import redemption as svc

router = APIRouter()
logger = logging.getLogger("thccb.redemption_admin")


# ===== 合作方 =====

@router.get("/partners", response_model=List[PartnerAdminItem])
async def list_partners(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    rows = list((await db.execute(select(RedemptionPartner))).scalars().all())
    return [PartnerAdminItem(**p.model_dump()) for p in rows]


@router.post("/partners", response_model=PartnerAdminItem)
async def create_partner(
    req: PartnerCreate,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    p = RedemptionPartner(**req.model_dump())
    db.add(p)
    await db.commit()
    await db.refresh(p)
    logger.info("REDEMPTION_PARTNER_CREATE admin=%s id=%s", admin.id, p.id)
    return PartnerAdminItem(**p.model_dump())


@router.patch("/partners/{partner_id}", response_model=PartnerAdminItem)
async def update_partner(
    partner_id: int,
    req: PartnerUpdate,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    p = await db.get(RedemptionPartner, partner_id)
    if p is None:
        raise HTTPException(status_code=404, detail="合作方不存在")
    for k, v in req.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return PartnerAdminItem(**p.model_dump())


# ===== 批次 =====

async def _batch_to_admin_item(db: AsyncSession, b: RedemptionBatch) -> BatchAdminItem:
    p = await db.get(RedemptionPartner, b.partner_id)
    total = await svc.count_total_for_batch(db, b.id)
    avail = await svc.count_available_for_batch(db, b.id)
    return BatchAdminItem(
        id=b.id, partner_id=b.partner_id,
        partner_name=p.name if p else "",
        name=b.name, description=b.description,
        unit_price=b.unit_price, status=b.status,
        total_count=total, sold_count=total - avail, available_count=avail,
        created_at=b.created_at,
    )


@router.get("/batches", response_model=List[BatchAdminItem])
async def list_batches_admin(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    rows = list((await db.execute(select(RedemptionBatch))).scalars().all())
    return [await _batch_to_admin_item(db, b) for b in rows]


@router.post("/batches", response_model=BatchAdminItem)
async def create_batch(
    req: BatchCreate,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    p = await db.get(RedemptionPartner, req.partner_id)
    if p is None:
        raise HTTPException(status_code=404, detail="合作方不存在")
    b = RedemptionBatch(
        partner_id=req.partner_id, name=req.name, description=req.description,
        unit_price=req.unit_price, status=BatchStatus.DRAFT,
        created_by_admin_id=admin.id,
    )
    db.add(b)
    await db.commit()
    await db.refresh(b)
    logger.info("REDEMPTION_BATCH_CREATE admin=%s id=%s", admin.id, b.id)
    return await _batch_to_admin_item(db, b)


@router.patch("/batches/{batch_id}", response_model=BatchAdminItem)
async def update_batch(
    batch_id: int,
    req: BatchUpdate,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    b = await db.get(RedemptionBatch, batch_id)
    if b is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    data = req.model_dump(exclude_unset=True)

    if "unit_price" in data and b.status == BatchStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="active 批次不允许修改价格，请新建批次")

    if "status" in data:
        new_status = data["status"]
        if new_status not in (BatchStatus.DRAFT, BatchStatus.ACTIVE, BatchStatus.ARCHIVED):
            raise HTTPException(status_code=400, detail="非法状态")

    for k, v in data.items():
        setattr(b, k, v)
    db.add(b)
    await db.commit()
    await db.refresh(b)
    return await _batch_to_admin_item(db, b)


# ===== CSV 导入 =====

@router.post("/batches/{batch_id}/import/preview", response_model=CsvImportPreview)
async def import_preview(
    batch_id: int,
    req: CsvImportRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    b = await db.get(RedemptionBatch, batch_id)
    if b is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    new, dup, invalid = await svc.import_codes_dry_run(db, batch_id, req.csv_text)
    return CsvImportPreview(
        total_lines=len(new) + len(dup) + len(invalid),
        new_codes=new, duplicate_codes=dup, invalid_codes=invalid,
    )


@router.post("/batches/{batch_id}/import/commit", response_model=CsvImportResult)
async def import_commit(
    batch_id: int,
    req: CsvImportConfirm,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    if not req.confirm:
        raise HTTPException(status_code=400, detail="必须确认")
    b = await db.get(RedemptionBatch, batch_id)
    if b is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    result = await svc.import_codes_commit(db, batch_id, req.csv_text)
    await db.commit()
    logger.info(
        "REDEMPTION_IMPORT admin=%s batch_id=%s inserted=%s dup=%s invalid=%s",
        admin.id, batch_id, result["inserted"],
        result["skipped_duplicate"], result["skipped_invalid"],
    )
    return CsvImportResult(**result)
