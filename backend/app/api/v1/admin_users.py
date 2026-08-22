"""管理员 · 用户 / 资金 / 贷款 / 账号 — 路由层。

挂载：/api/v1/admin/users（见 app/main.py）。业务逻辑全部在
app/services/admin_user_service.py；这里只做鉴权、入参校验、错误翻译、审计日志。
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.base import User
from app.schemas.loan import ForceLoanRequest, ForgiveDebtRequest
from app.services import admin_user_service as svc
from app.services import title_service
from app.services.admin_user_service import AdminUserError, UserFilter

logger = logging.getLogger(__name__)
_loan_admin_logger = logging.getLogger("thccb.loan_admin")

router = APIRouter()


def _http(e: AdminUserError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.detail)


# ── 查询 ─────────────────────────────────────

@router.get("", summary="用户列表（仅管理员）")
async def list_users(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    users = (await db.execute(select(User).order_by(User.id.asc()).limit(200))).scalars().all()
    return [
        {
            "id": u.id, "username": u.username,
            "cash": svc._money(u.cash), "debt": svc._money(u.debt),
            "is_active": u.is_active, "is_superuser": u.is_superuser,
        }
        for u in users
    ]


@router.get("/{user_id}", summary="用户快照（仅管理员）")
async def get_user(
    user_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="user not found")
    equipped = await title_service.get_equipped_chip(db, user_id)
    return {
        "user_id": u.id, "username": u.username, "email": u.email,
        "cash": float(u.cash), "debt": float(u.debt),
        "debt_last_accrued_at": u.debt_last_accrued_at,
        "is_active": u.is_active, "is_superuser": u.is_superuser,
        "equipped_title_id": u.equipped_title_id,
        "equipped_title": (
            {"id": equipped.id, "name": equipped.name, "color": equipped.color, "icon": equipped.icon}
            if equipped else None
        ),
    }


# ── 资金 / 贷款 ───────────────────────────────

class AdjustCashRequest(BaseModel):
    amount: Decimal = Field(..., description="正数加钱，负数扣钱")
    reason: str = Field(default="", max_length=200, description="操作原因备注")


@router.post("/{user_id}/cash", summary="调整用户现金（仅管理员）")
async def adjust_cash(
    user_id: int, req: AdjustCashRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        r = await svc.adjust_cash(db, target_id=user_id, amount=req.amount, reason=req.reason, admin_id=admin.id)
    except AdminUserError as e:
        raise _http(e)
    logger.info("ADJUST_CASH admin_id=%s user_id=%s amount=%s reason=%s new_cash=%s",
                admin.id, user_id, req.amount, req.reason, r["new_cash"])
    return r


@router.post("/{user_id}/loan", summary="管理员强制放贷")
async def force_loan(
    user_id: int, req: ForceLoanRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        r = await svc.force_loan(db, target_id=user_id, amount=Decimal(req.amount), reason=req.reason, admin_id=admin.id)
    except AdminUserError as e:
        raise _http(e)
    _loan_admin_logger.info("FORCE_LOAN admin_id=%s user_id=%s amount=%s reason=%s new_cash=%s new_debt=%s",
                            admin.id, user_id, req.amount, req.reason, r["cash"], r["debt"])
    return r


@router.post("/{user_id}/forgive-debt", summary="管理员免除债务")
async def forgive_debt(
    user_id: int, req: ForgiveDebtRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        r = await svc.forgive_debt(db, target_id=user_id, amount=Decimal(req.amount), reason=req.reason, admin_id=admin.id)
    except AdminUserError as e:
        raise _http(e)
    _loan_admin_logger.info("FORGIVE_DEBT admin_id=%s user_id=%s requested=%s effective=%s reason=%s new_debt=%s",
                            admin.id, user_id, req.amount, r["effective"], req.reason, r["debt"])
    return r


# ── 账号 ─────────────────────────────────────

class SetRoleRequest(BaseModel):
    is_admin: bool = Field(..., description="True=提升为管理员；False=取消管理员")


@router.patch("/{user_id}/role", summary="设置/取消管理员权限（仅管理员）")
async def set_role(
    user_id: int, req: SetRoleRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        r = await svc.set_role(db, target_id=user_id, is_admin=req.is_admin, admin_id=admin.id)
    except AdminUserError as e:
        raise _http(e)
    if r["changed"]:
        logger.info("SET_ADMIN by_admin_id=%s target_user_id=%s target_username=%s new_is_admin=%s",
                    admin.id, user_id, r["username"], req.is_admin)
    return r


class BanUserRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500, description="封号原因（选填，写入 log 审计）")
    related_suspicion_id: Optional[int] = Field(default=None, description="关联的 BotSuspicion ID（选填）")


@router.patch("/{user_id}/ban", summary="封号（仅管理员）")
async def ban_user(
    user_id: int, req: BanUserRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        r = await svc.ban(db, target_id=user_id, admin_id=admin.id, reason=req.reason)
    except AdminUserError as e:
        raise _http(e)
    logger.warning("ADMIN_BAN_USER admin_id=%s target_user_id=%s target_username=%s was_active=%s reason=%s related_suspicion_id=%s",
                   admin.id, user_id, r["username"], r["changed"], req.reason or "<none>", req.related_suspicion_id)
    return r


@router.patch("/{user_id}/unban", summary="解封（仅管理员）")
async def unban_user(
    user_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        r = await svc.unban(db, target_id=user_id, admin_id=admin.id)
    except AdminUserError as e:
        raise _http(e)
    logger.warning("ADMIN_UNBAN_USER admin_id=%s target_user_id=%s target_username=%s was_active=%s",
                   admin.id, user_id, r["username"], not r["changed"])
    return r


# ── 批量 ─────────────────────────────────────
# 流程：先 dry_run=true 拿预览 → 前端二次确认 → dry_run=false 执行。

class BatchAdjustCashRequest(BaseModel):
    filter: UserFilter
    amount: Decimal = Field(..., description="正数加钱，负数扣钱；不能为 0")
    reason: str = Field(..., min_length=1, max_length=200, description="审计原因，必填")
    dry_run: bool = Field(default=True)


@router.post("/batch/adjust-cash", summary="批量调整用户现金（仅管理员）")
async def batch_adjust_cash(
    req: BatchAdjustCashRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        r = await svc.batch_adjust_cash(db, f=req.filter, amount=req.amount, reason=req.reason,
                                        admin_id=admin.id, dry_run=req.dry_run)
    except AdminUserError as e:
        raise _http(e)
    if not req.dry_run:
        for row in r["updated"]:
            logger.info("BATCH_ADJUST_CASH admin_id=%s user_id=%s amount=%s reason=%s new_cash=%s",
                        admin.id, row["user_id"], req.amount, req.reason, row["cash_after"])
        if r["failed"]:
            logger.warning("BATCH_ADJUST_CASH admin_id=%s skipped_count=%s amount=%s reason=%s",
                           admin.id, len(r["failed"]), req.amount, req.reason)
    return r


class AmnestyRequest(BaseModel):
    filter: UserFilter
    reset_cash_to: Optional[Decimal] = Field(default=None, description="None → site_config.initial_balance")
    forgive_debt: bool = Field(default=True)
    reason: str = Field(..., min_length=1, max_length=200, description="审计原因，必填")
    dry_run: bool = Field(default=True)


@router.post("/batch/amnesty", summary="大赦天下：清债 + 现金还原到初始（仅管理员）")
async def amnesty(
    req: AmnestyRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        r = await svc.amnesty(db, f=req.filter, reset_cash_to=req.reset_cash_to,
                              forgive_debt=req.forgive_debt, reason=req.reason,
                              admin_id=admin.id, dry_run=req.dry_run)
    except AdminUserError as e:
        raise _http(e)
    if not req.dry_run:
        for row in r["updated"]:
            _loan_admin_logger.info(
                "AMNESTY admin_id=%s user_id=%s cash %s->%s debt %s->%s forgiven=%s reason=%s",
                admin.id, row["user_id"], row["cash_before"], row["cash_after"],
                row["debt_before"], row["debt_after"], row["debt_forgiven"], req.reason,
            )
        logger.warning("AMNESTY_DONE admin_id=%s count=%s total_cash_delta=%s total_debt_forgiven=%s reason=%s",
                       admin.id, r["updated_count"], r["total_cash_delta"], r["total_debt_forgiven"], req.reason)
    return r
