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
    UserTitleGrantRequest, UserTitleListItem,
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


@router.get("/users/{user_id}/titles", response_model=List[UserTitleListItem],
            summary="列出用户的全部 title")
async def admin_list_user_titles(
    user_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    rows = await title_service.list_my_titles(db, user_id)
    return [
        UserTitleListItem(
            title=_to_title_read(t),
            granted_at=ut.granted_at,
            source=ut.source,
            granted_by_admin_id=ut.granted_by_admin_id,
        ) for ut, t in rows
    ]


@router.post("/users/{user_id}/titles", summary="授予 title")
async def admin_grant_title(
    user_id: int,
    req: UserTitleGrantRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    await title_service.grant_user_title(db, user_id, req.title_id, admin.id)
    return {"user_id": user_id, "title_id": req.title_id}


@router.delete("/users/{user_id}/titles/{title_id}", summary="撤销 title")
async def admin_revoke_title(
    user_id: int, title_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    await title_service.revoke_user_title(db, user_id, title_id)
    return {"user_id": user_id, "title_id": title_id, "revoked": True}


@router.get("/users/{user_id}/summary", summary="资产快照（admin 查任意用户）")
async def admin_user_summary(
    user_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    """Admin 查任意用户。最小实现：只返回 user 表基础字段。"""
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="user not found")
    return {
        "user_id": u.id,
        "username": u.username,
        "email": u.email,
        "cash": float(u.cash),
        "debt": float(u.debt),
        "is_active": u.is_active,
        "is_superuser": u.is_superuser,
        "equipped_title_id": u.equipped_title_id,
    }
