"""Title 用户自助接口 — 我的称号 / 公开 catalog / 别人佩戴的 chip。"""
from typing import Optional, List
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.users import current_active_user
from app.models.base import User
from app.models.title import Title
from app.schemas.title import (
    TitleRead, TitleChipRead, MyTitlesResponse, MyTitleItem, EquipRequest,
    RedeemRequest, RedeemResponse,
)
from app.services import title_service, title_code_service

router = APIRouter()


def _to_chip(t) -> TitleChipRead:
    return TitleChipRead(id=t.id, name=t.name, color=t.color, icon=t.icon)


def _to_full(t) -> TitleRead:
    return TitleRead(
        id=t.id, name=t.name, description=t.description,
        color=t.color, icon=t.icon, sort_order=t.sort_order,
        is_active=t.is_active, created_at=t.created_at,
    )


@router.get("/me", response_model=MyTitlesResponse, summary="我的称号")
async def my_titles(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    rows = await title_service.list_my_titles(db, user.id)
    items = [
        MyTitleItem(
            title=_to_full(t),
            granted_at=ut.granted_at,
            source=ut.source,
        ) for ut, t in rows
    ]
    return MyTitlesResponse(
        equipped_title_id=user.equipped_title_id,
        titles=items,
    )


@router.get("/catalog", response_model=List[TitleRead], summary="公开 title catalog")
async def public_catalog(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    titles = await title_service.list_active_catalog(db)
    return [_to_full(t) for t in titles]


@router.get("/users/{user_id}/equipped", response_model=Optional[TitleChipRead],
            summary="某用户当前佩戴的 title")
async def user_equipped(
    user_id: int,
    requester: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    t = await title_service.get_equipped_chip(db, user_id)
    return _to_chip(t) if t else None


@router.post("/me/equip", summary="佩戴 / 取下称号")
async def equip(
    req: EquipRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    await title_service.equip_title(db, user.id, req.title_id)
    return {"equipped_title_id": req.title_id}


@router.post("/redeem", response_model=RedeemResponse, summary="兑换激活码")
async def redeem(
    req: RedeemRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    t = await title_code_service.redeem_code(db, user.id, req.code)
    return RedeemResponse(title=_to_full(t))
