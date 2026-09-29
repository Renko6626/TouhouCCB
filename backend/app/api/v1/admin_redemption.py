"""兑换中心管理员端 API。"""
from __future__ import annotations
import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from sqlalchemy.orm import aliased

from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.base import User
from app.models.redemption import (
    RedemptionPartner, RedemptionBatch, RedemptionCode, BatchStatus, CodeStatus,
)
from app.schemas.redemption import (
    PartnerCreate, PartnerUpdate, PartnerAdminItem,
    BatchCreate, BatchUpdate, BatchAdminItem,
    CsvImportRequest, CsvImportPreview,
    CsvImportConfirm, CsvImportResult,
    CodeAdminItem, CodeAdminPage, CodeRedeemRequest, CodeRevokeRequest,
)
from app.services import redemption as svc

router = APIRouter()
logger = logging.getLogger("thccb.redemption_admin")

# CSV 导入安全上限：防止恶意/误操作 POST 巨大 payload 撑爆请求体
# 平均一行码 ~32B，5000 行 ~160KB，远低于 FastAPI 默认上限，但已远超合理批次规模
_MAX_CSV_BYTES = 256 * 1024
_MAX_CSV_LINES = 5000


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
    redeemed = await svc.count_redeemed_for_batch(db, b.id)
    return BatchAdminItem(
        id=b.id, partner_id=b.partner_id,
        partner_name=p.name if p else "",
        name=b.name, description=b.description,
        unit_price=b.unit_price, status=b.status,
        total_count=total, sold_count=total - avail, available_count=avail, redeemed_count=redeemed,
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

def _enforce_csv_limits(csv_text: str) -> None:
    if len(csv_text.encode("utf-8")) > _MAX_CSV_BYTES:
        raise HTTPException(status_code=413, detail=f"CSV 超过 {_MAX_CSV_BYTES // 1024} KB 上限")
    if csv_text.count("\n") > _MAX_CSV_LINES:
        raise HTTPException(status_code=413, detail=f"CSV 超过 {_MAX_CSV_LINES} 行上限")


@router.post("/batches/{batch_id}/import/preview", response_model=CsvImportPreview)
async def import_preview(
    batch_id: int,
    req: CsvImportRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    _enforce_csv_limits(req.csv_text)
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
    _enforce_csv_limits(req.csv_text)
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


# ===== 线下兑换码核销（仅管理员；工作人员共享此权威状态） =====

def _admin_code_query():
    buyer = aliased(User)
    staff = aliased(User)
    return (
        select(RedemptionCode, RedemptionBatch.name, RedemptionPartner.name,
               buyer.username, staff.username)
        .join(RedemptionBatch, RedemptionBatch.id == RedemptionCode.batch_id)
        .join(RedemptionPartner, RedemptionPartner.id == RedemptionBatch.partner_id)
        .outerjoin(buyer, buyer.id == RedemptionCode.bought_by_user_id)
        .outerjoin(staff, staff.id == RedemptionCode.redeemed_by_admin_id)
    )


def _admin_code_item(row) -> CodeAdminItem:
    code, batch_name, partner_name, buyer_name, staff_name = row
    return CodeAdminItem(
        id=code.id, batch_id=code.batch_id, batch_name=batch_name, partner_name=partner_name,
        code_string=code.code_string, status=code.status,
        bought_by_user_id=code.bought_by_user_id, bought_by_username=buyer_name,
        bought_at=code.bought_at, redeemed_at=code.redeemed_at,
        redeemed_by_admin_id=code.redeemed_by_admin_id, redeemed_by_admin_username=staff_name,
        redemption_note=code.redemption_note,
    )


async def _get_admin_code(db: AsyncSession, code_id: int) -> CodeAdminItem:
    row = (await db.execute(
        _admin_code_query().where(RedemptionCode.id == code_id)
        .execution_options(populate_existing=True)
    )).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="兑换码不存在")
    return _admin_code_item(row)


@router.get("/codes", response_model=CodeAdminPage)
async def list_codes_admin(
    batch_id: Optional[int] = Query(default=None, ge=1),
    q: str = Query(default="", max_length=128),
    status: Literal["all", "available", "pending", "redeemed"] = "all",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    if batch_id is not None and await db.get(RedemptionBatch, batch_id) is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    filters = []
    if batch_id is not None:
        filters.append(RedemptionCode.batch_id == batch_id)
    if q.strip():
        filters.append(func.lower(RedemptionCode.code_string).contains(q.strip().lower(), autoescape=True))
    if status == "available":
        filters.append(RedemptionCode.status == CodeStatus.AVAILABLE)
    elif status == "pending":
        filters.extend([RedemptionCode.status == CodeStatus.SOLD, RedemptionCode.redeemed_at.is_(None)])
    elif status == "redeemed":
        filters.append(RedemptionCode.redeemed_at.is_not(None))
    total = int((await db.execute(
        select(func.count()).select_from(RedemptionCode).where(*filters)
    )).scalar_one())
    rows = (await db.execute(
        _admin_code_query().where(*filters).order_by(RedemptionCode.id)
        .offset((page - 1) * page_size).limit(page_size)
    )).all()
    return CodeAdminPage(items=[_admin_code_item(row) for row in rows], total=total, page=page, page_size=page_size)


@router.post("/codes/{code_id}/redeem", response_model=CodeAdminItem)
async def redeem_code_admin(
    code_id: int,
    req: CodeRedeemRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    await svc.fulfill_code(db, code_id, admin.id, req.note)
    await db.commit()
    return await _get_admin_code(db, code_id)


@router.post("/codes/{code_id}/revoke", response_model=CodeAdminItem)
async def revoke_code_admin(
    code_id: int,
    req: CodeRevokeRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    await svc.revoke_fulfillment(db, code_id, admin.id, req.reason, req.expected_redeemed_at)
    await db.commit()
    return await _get_admin_code(db, code_id)
