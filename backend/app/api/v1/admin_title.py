"""Title 管理后端路由 — admin only。

挂载位置见 app/main.py: prefix="/api/v1/admin"
"""
from typing import List
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.base import User
from app.models.title import Title
from app.schemas.title import (
    TitleRead, TitleCreateRequest, TitleUpdateRequest,
    BatchCreateRequest, BatchRead, CSVImportResponse,
)
from app.services import title_service, title_code_service

router = APIRouter()


def _to_title_read(t: Title) -> TitleRead:
    return TitleRead(
        id=t.id, name=t.name, description=t.description,
        color=t.color, icon=t.icon, sort_order=t.sort_order,
        is_active=t.is_active, created_at=t.created_at,
    )


@router.get("/titles", response_model=List[TitleRead], summary="列出全部 title")
async def list_titles(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    titles = await title_service.list_titles(db, include_inactive=True)
    return [_to_title_read(t) for t in titles]


@router.post("/titles", response_model=TitleRead, summary="创建 title")
async def create_title(
    req: TitleCreateRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    t = await title_service.create_title(db, req)
    return _to_title_read(t)


@router.patch("/titles/{title_id}", response_model=TitleRead, summary="修改 title")
async def update_title(
    title_id: int,
    req: TitleUpdateRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    t = await title_service.update_title(db, title_id, req)
    return _to_title_read(t)


@router.get("/title-batches", response_model=List[BatchRead], summary="列出 batch")
async def list_title_batches(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    rows = await title_code_service.list_batches_with_counts(db)
    return [BatchRead(**r) for r in rows]


@router.post("/title-batches", response_model=BatchRead, summary="新建 batch")
async def create_title_batch(
    req: BatchCreateRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    b = await title_code_service.create_batch(
        db, req.title_id, req.name, req.description, admin.id,
    )
    t = await db.get(Title, b.title_id)
    return BatchRead(
        id=b.id, title_id=b.title_id, title_name=t.name,
        name=b.name, description=b.description,
        total=0, used=0, created_at=b.created_at,
    )


@router.post("/title-batches/{batch_id}/import-codes",
             response_model=CSVImportResponse, summary="CSV 导入激活码到批次")
async def import_codes(
    batch_id: int,
    file: UploadFile = File(...),
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    raw = await file.read()
    if len(raw) > 1 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="CSV 文件超过 1MB 上限")
    codes = title_code_service.parse_csv_codes(raw)
    inserted = await title_code_service.import_codes_to_batch(db, batch_id, codes)
    return CSVImportResponse(inserted=inserted)
