"""Title catalog 业务逻辑（admin 端 CRUD + 状态查询）。

CLAUDE.md 守则：不让外层路由直接操纵 ORM；service 收口业务规则。
"""
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException, status

from app.models.title import Title
from app.schemas.title import TitleCreateRequest, TitleUpdateRequest


async def list_titles(db: AsyncSession, include_inactive: bool = True) -> List[Title]:
    stmt = select(Title)
    if not include_inactive:
        stmt = stmt.where(Title.is_active == True)  # noqa: E712
    stmt = stmt.order_by(Title.sort_order.asc(), Title.id.asc())
    return list((await db.execute(stmt)).scalars().all())


async def create_title(db: AsyncSession, req: TitleCreateRequest) -> Title:
    dup = (await db.execute(select(Title).where(Title.name == req.name))).scalar_one_or_none()
    if dup:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"称号名 '{req.name}' 已存在")
    t = Title(
        name=req.name, description=req.description,
        color=req.color, icon=req.icon, sort_order=req.sort_order,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


async def update_title(
    db: AsyncSession, title_id: int, req: TitleUpdateRequest,
) -> Title:
    t = await db.get(Title, title_id)
    if not t:
        raise HTTPException(status_code=404, detail="title not found")
    if req.name is not None and req.name != t.name:
        dup = (await db.execute(select(Title).where(Title.name == req.name))).scalar_one_or_none()
        if dup:
            raise HTTPException(status_code=409, detail=f"称号名 '{req.name}' 已存在")
        t.name = req.name
    for f in ("description", "color", "icon", "sort_order", "is_active"):
        v = getattr(req, f)
        if v is not None:
            setattr(t, f, v)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


async def get_title_or_404(db: AsyncSession, title_id: int) -> Title:
    t = await db.get(Title, title_id)
    if not t:
        raise HTTPException(status_code=404, detail="title not found")
    return t
