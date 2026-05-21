"""Bot 预警审核 + 封号 admin endpoints.

跟 SQLAdmin BotSuspicionAdmin (core/admin.py:99) 并存：SQLAdmin 是后台运维直查
原始记录；这里给前端 BotReviewBan.vue 提供 JSON API 做"列表+一键处置"工作流。
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import List, Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session, managed_transaction
from app.core.users import current_superuser
from app.models.base import BotSuspicion, User

router = APIRouter()
logger = logging.getLogger("thccb.admin_bot")


# ── Schema ───────────────────────────────────────────────────────────────

class BotSuspicionItem(BaseModel):
    """Bot 预警列表项，含关联 user 的 username + is_active 状态。"""
    id: int
    user_id: int
    username: str
    user_is_active: bool   # False = 已被封号
    triggered_at: datetime
    signals: str
    metrics: str
    window_start: datetime
    window_end: datetime
    review_status: str
    reviewed_by: Optional[int]
    reviewed_at: Optional[datetime]
    review_note: Optional[str]


class ReviewSuspicionRequest(BaseModel):
    status: Literal["confirmed_bot", "false_positive", "pending"] = Field(
        ..., description="审核结论"
    )
    note: Optional[str] = Field(default=None, max_length=500)


class BannedUserItem(BaseModel):
    user_id: int
    username: str
    casdoor_id: Optional[str]


# ── Endpoints ────────────────────────────────────────────────────────────

@router.get("/suspicions", response_model=List[BotSuspicionItem],
            summary="Bot 预警列表（仅管理员）")
async def list_suspicions(
    status: Literal["pending", "reviewed", "all"] = Query(
        "pending", description="筛选状态：pending=待审/reviewed=已审/all=全部"
    ),
    limit: int = Query(100, ge=1, le=500),
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    stmt = (
        select(BotSuspicion, User.username, User.is_active)
        .join(User, User.id == BotSuspicion.user_id)
        .order_by(BotSuspicion.triggered_at.desc())
        .limit(limit)
    )
    if status == "pending":
        stmt = stmt.where(BotSuspicion.review_status == "pending")
    elif status == "reviewed":
        stmt = stmt.where(BotSuspicion.review_status != "pending")
    # status == "all" → 不过滤

    rows = (await db.execute(stmt)).all()
    return [
        BotSuspicionItem(
            id=int(s.id),
            user_id=int(s.user_id),
            username=username,
            user_is_active=bool(user_is_active),
            triggered_at=(
                s.triggered_at.replace(tzinfo=timezone.utc)
                if s.triggered_at.tzinfo is None else s.triggered_at
            ),
            signals=s.signals,
            metrics=s.metrics,
            window_start=s.window_start,
            window_end=s.window_end,
            review_status=s.review_status,
            reviewed_by=s.reviewed_by,
            reviewed_at=s.reviewed_at,
            review_note=s.review_note,
        )
        for s, username, user_is_active in rows
    ]


@router.patch("/suspicions/{suspicion_id}/review",
              summary="审核 Bot 预警（仅管理员）")
async def review_suspicion(
    suspicion_id: int,
    req: ReviewSuspicionRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    async with managed_transaction(db):
        target = (await db.execute(
            select(BotSuspicion).where(BotSuspicion.id == suspicion_id).with_for_update()
        )).scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=404, detail="预警记录不存在")

        target.review_status = req.status
        target.reviewed_by = admin.id
        target.reviewed_at = datetime.now(timezone.utc)
        if req.note is not None:
            target.review_note = req.note

    logger.info(
        "ADMIN_REVIEW_SUSPICION admin_id=%s suspicion_id=%s user_id=%s status=%s note=%s",
        admin.id, suspicion_id, target.user_id, req.status, req.note or "<none>",
    )
    return {
        "id": suspicion_id,
        "review_status": target.review_status,
        "reviewed_by": target.reviewed_by,
        "reviewed_at": target.reviewed_at.isoformat() if target.reviewed_at else None,
        "review_note": target.review_note,
    }


@router.get("/banned-users", response_model=List[BannedUserItem],
            summary="已封禁用户列表（仅管理员）")
async def list_banned_users(
    limit: int = Query(200, ge=1, le=1000),
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    stmt = (
        select(User.id, User.username, User.casdoor_id)
        .where(User.is_active == False)  # noqa: E712
        .order_by(User.id.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        BannedUserItem(user_id=int(uid), username=uname, casdoor_id=cid)
        for uid, uname, cid in rows
    ]


@router.get("/stats", summary="Bot 审核统计（仅管理员）")
async def review_stats(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    """前端 panel header 显示用："待审 X 条 / 已封 Y 人"。"""
    pending_count = int((await db.execute(
        select(func.count()).select_from(BotSuspicion)
        .where(BotSuspicion.review_status == "pending")
    )).scalar_one())
    banned_count = int((await db.execute(
        select(func.count()).select_from(User)
        .where(User.is_active == False)  # noqa: E712
    )).scalar_one())
    return {
        "pending_suspicions": pending_count,
        "banned_users": banned_count,
    }
