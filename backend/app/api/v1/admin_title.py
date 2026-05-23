"""Title 管理后端路由 — admin only。

挂载位置见 app/main.py: prefix="/api/v1/admin"
"""
from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.base import User
from app.models.title import Title
from app.schemas.title import (
    TitleRead, TitleCreateRequest, TitleUpdateRequest,
)
from app.services import title_service

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
