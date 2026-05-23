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


async def list_my_titles(db: AsyncSession, user_id: int):
    """返回某用户的全部 title (UserTitle, Title) tuple，按 sort_order asc。

    显式 join 避免 raise_on_sql 反向集合触发。
    """
    from app.models.title import UserTitle
    stmt = (
        select(UserTitle, Title)
        .join(Title, UserTitle.title_id == Title.id)
        .where(UserTitle.user_id == user_id)
        .order_by(Title.sort_order.asc(), Title.id.asc())
    )
    rows = (await db.execute(stmt)).all()
    return rows


async def get_equipped_chip(db: AsyncSession, user_id: int):
    """读某用户当前佩戴的 title（chip 视图）。无佩戴返回 None。"""
    from app.models.base import User
    stmt = (
        select(Title)
        .join(User, User.equipped_title_id == Title.id)
        .where(User.id == user_id)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_active_catalog(db: AsyncSession) -> List[Title]:
    return await list_titles(db, include_inactive=False)


async def equip_title(db: AsyncSession, user_id: int, title_id: Optional[int]) -> None:
    """切换用户佩戴的 title。title_id=None → 取下。

    安全约束：title_id 非 None 时，必须确认用户已持有该 title。
    """
    from app.models.base import User
    from app.models.title import UserTitle
    u = await db.get(User, user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    if title_id is None:
        u.equipped_title_id = None
        db.add(u)
        await db.commit()
        return
    own = (await db.execute(
        select(UserTitle).where(
            UserTitle.user_id == user_id, UserTitle.title_id == title_id,
        )
    )).scalar_one_or_none()
    if own is None:
        raise HTTPException(status_code=403, detail="你未持有此称号")
    u.equipped_title_id = title_id
    db.add(u)
    await db.commit()


async def grant_user_title(
    db: AsyncSession, user_id: int, title_id: int, admin_id: int,
) -> None:
    from app.models.base import User
    from app.models.title import UserTitle
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="user not found")
    t = await db.get(Title, title_id)
    if not t:
        raise HTTPException(status_code=404, detail="title not found")
    if not t.is_active:
        raise HTTPException(status_code=400, detail="该称号已软删，不能授予")
    existing = (await db.execute(
        select(UserTitle).where(
            UserTitle.user_id == user_id, UserTitle.title_id == title_id,
        )
    )).scalar_one_or_none()
    if existing:
        return  # 幂等
    db.add(UserTitle(
        user_id=user_id, title_id=title_id,
        granted_by_admin_id=admin_id, source="admin",
    ))
    await db.commit()


async def revoke_user_title(
    db: AsyncSession, user_id: int, title_id: int,
) -> None:
    from app.models.base import User
    from app.models.title import UserTitle
    ut = (await db.execute(
        select(UserTitle).where(
            UserTitle.user_id == user_id, UserTitle.title_id == title_id,
        )
    )).scalar_one_or_none()
    if not ut:
        raise HTTPException(status_code=404, detail="该用户未持有此称号")
    u = await db.get(User, user_id)
    if u and u.equipped_title_id == title_id:
        u.equipped_title_id = None
        db.add(u)
    await db.delete(ut)
    await db.commit()
