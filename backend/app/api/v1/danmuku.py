"""弹幕兑换用户端 API。"""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_async_session
from app.core.users import current_active_user
from app.models.base import User
from app.models.redemption import DanmukuExchange
from app.schemas.danmuku import (
    DanmukuExchangeRequest,
    DanmukuExchangeResponse,
    DanmukuExchangeHistoryItem,
)
from app.services import danmuku as svc

router = APIRouter()
logger = logging.getLogger("thccb.danmuku")


@router.post("/exchange", response_model=DanmukuExchangeResponse)
async def exchange(
    req: DanmukuExchangeRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    """用站内现金兑换弹幕系统激活码（yuan/huo 1:1 站内现金）。"""
    try:
        result = await svc.exchange(
            db,
            user_id=user.id,
            qq_user_id=req.qq_user_id,
            room_id=req.room_id,
            yuan=req.yuan,
            huo=req.huo,
        )
    except svc.ExchangeError as e:
        await db.rollback()
        if e.code == "INSUFFICIENT_CASH":
            raise HTTPException(status_code=422, detail="现金不足")
        if e.code == "INVALID_AMOUNT":
            raise HTTPException(status_code=422, detail=str(e))
        raise
    await db.commit()
    logger.info(
        "DANMUKU_EXCHANGE user_id=%s qq=%s room=%s yuan=%s huo=%s amount=%s",
        user.id, req.qq_user_id, req.room_id, req.yuan, req.huo, result.amount,
    )
    return DanmukuExchangeResponse(
        id=result.id,
        code_string=result.code_string,
        yuan=result.yuan,
        huo=result.huo,
        amount=result.amount,
        cash_after=result.cash_after,
        timestamp=result.timestamp,
    )


@router.get("/my", response_model=List[DanmukuExchangeHistoryItem])
async def my_history(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    """查自己的弹幕兑换历史，按时间倒序。"""
    stmt = (
        select(DanmukuExchange)
        .where(DanmukuExchange.user_id == user.id)
        .order_by(DanmukuExchange.timestamp.desc())
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [
        DanmukuExchangeHistoryItem(
            id=r.id,
            qq_user_id=r.qq_user_id,
            room_id=r.room_id,
            yuan=r.yuan,
            huo=r.huo,
            amount=r.amount,
            code_string=r.code_string,
            timestamp=r.timestamp,
        )
        for r in rows
    ]
