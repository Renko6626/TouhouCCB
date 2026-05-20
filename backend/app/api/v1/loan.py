"""Loan 玩家接口。所有 handler 为 loan_service 薄封装。"""
from __future__ import annotations
import logging
from datetime import timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.database import get_async_session
from app.core.users import current_active_user
from app.models.base import User, LiquidationEvent
from app.schemas.loan import LoanQuotaResponse, BorrowRequest, LoanActionResponse, RepayRequest
from app.services import site_config, loan_service
from app.services.wealth import compute_users_holdings_value

router = APIRouter()
logger = logging.getLogger("thccb.loan")


async def _holdings_value(db: AsyncSession, user_id: int) -> Decimal:
    """借款相关接口的持仓估值 —— 用 LCV (立即清算价值) 保守口径。

    历史上这里用 MTM (瞬时价 × 数量)，导致借款页 NW 偏高、Portfolio NW 偏低的
    分裂体感。统一改 LCV 后：借款额度更保守（按可变现金额算 max_borrow），
    避免用户被 MTM 高估值"骗"出超出真实清算能力的杠杆。详见
    docs/holdings-value-semantics.md。
    """
    return (
        await compute_users_holdings_value(db, user_ids=[user_id])
    ).get(user_id, Decimal("0"))


@router.get("/quota", response_model=LoanQuotaResponse)
async def get_quota(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    enabled = await site_config.get_bool(db, "loan_enabled")
    k = await site_config.get_decimal(db, "loan_leverage_k")
    rate = await site_config.get_decimal(db, "loan_daily_rate")
    hv = await _holdings_value(db, user.id)
    net_worth = (user.cash - user.debt + hv).quantize(Decimal("0.000001"))
    max_borrow = loan_service.compute_max_borrow(user, hv, k)
    return LoanQuotaResponse(
        enabled=enabled,
        cash=user.cash,
        debt=user.debt,
        net_worth=net_worth,
        leverage_k=k,
        daily_rate=rate,
        max_borrow=max_borrow,
        last_accrued_at=user.debt_last_accrued_at,
    )


@router.post("/borrow", response_model=LoanActionResponse)
async def borrow(
    req: BorrowRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    enabled = await site_config.get_bool(db, "loan_enabled")
    if not enabled:
        raise HTTPException(status_code=403, detail="借款功能已关闭")

    k = await site_config.get_decimal(db, "loan_leverage_k")
    rate = await site_config.get_decimal(db, "loan_daily_rate")
    amount = Decimal(req.amount)

    # 预检查 quota（锁内 increase_debt 会重算 debt，边界仍由 amount + 已有 debt 控制）
    hv = await _holdings_value(db, user.id)
    max_borrow = loan_service.compute_max_borrow(user, hv, k)
    if amount > max_borrow:
        raise HTTPException(
            status_code=400,
            detail=f"借款额超出额度（可借 {max_borrow}，申请 {amount}）",
        )

    u = await loan_service.increase_debt(
        db, user.id, amount, grant_cash=True, daily_rate=rate,
    )
    await db.commit()
    await db.refresh(u)
    logger.info(
        "LOAN_BORROW user_id=%s amount=%s new_cash=%s new_debt=%s",
        user.id, amount, u.cash, u.debt,
    )
    hv2 = await _holdings_value(db, u.id)
    return LoanActionResponse(
        cash=u.cash,
        debt=u.debt,
        max_borrow=loan_service.compute_max_borrow(u, hv2, k),
    )


@router.post("/repay", response_model=LoanActionResponse)
async def repay(
    req: RepayRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    rate = await site_config.get_decimal(db, "loan_daily_rate")
    k = await site_config.get_decimal(db, "loan_leverage_k")
    amount = Decimal(req.amount)

    # 不做 pre-check：服务层会在锁内 accrue 后用 min(amount, 真实 debt, 真实 cash) 封顶。
    # 这样：(1) 不会因复利让 cash 跑负 (2) 用户输入超额（>debt 或 >cash）会被静默封顶，
    # 实际扣减由 effective 字段返回，前端可展示"实际还款 金 N"。
    if user.cash <= 0 and user.debt > 0:
        raise HTTPException(status_code=400, detail="现金为 0，无法还款；请先卖出持仓变现")

    try:
        u, effective = await loan_service.decrease_debt(
            db, user.id, amount, consume_cash=True, daily_rate=rate,
        )
    except (ValueError, loan_service.LoanServiceError) as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    await db.commit()
    await db.refresh(u)
    logger.info(
        "LOAN_REPAY user_id=%s requested=%s effective=%s new_cash=%s new_debt=%s",
        user.id, amount, effective, u.cash, u.debt,
    )
    hv = await _holdings_value(db, u.id)
    return LoanActionResponse(
        cash=u.cash,
        debt=u.debt,
        max_borrow=loan_service.compute_max_borrow(u, hv, k),
        effective=effective,
    )


@router.get("/recent-liquidations", summary="最近强平记录（公开，首页展示）")
async def recent_liquidations(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_session),
):
    """匿名可访问。教育警示用。"""
    stmt = (
        select(LiquidationEvent, User.username)
        .join(User, User.id == LiquidationEvent.user_id)
        .order_by(LiquidationEvent.triggered_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "id": int(ev.id),
            "username": username,
            "triggered_at": (
                ev.triggered_at.replace(tzinfo=timezone.utc)
                if ev.triggered_at.tzinfo is None else ev.triggered_at
            ).isoformat(),
            "pre_cash": float(ev.pre_cash),
            "pre_debt": float(ev.pre_debt),
            "pre_holdings_value": float(ev.pre_holdings_value),
            "pre_net_worth": float(ev.pre_net_worth),
            "pre_margin_ratio": float(ev.pre_margin_ratio)
                if ev.pre_margin_ratio is not None else None,
            "total_proceeds": float(ev.total_proceeds),
            "repaid_amount": float(ev.repaid_amount),
            "remaining_debt": float(ev.remaining_debt),
            "post_cash": float(ev.post_cash),
            "fully_liquidated": ev.remaining_debt == Decimal("0"),
            "trigger_source": ev.trigger_source,
            "mode": ev.mode,
        }
        for ev, username in rows
    ]


@router.get("/liquidation-policy", summary="当前强制平仓策略（公开，贷款页说明用）")
async def liquidation_policy(
    db: AsyncSession = Depends(get_async_session),
):
    """匿名可访问。前端教育/说明展示用，实时读 site_config 让 admin 调整立刻反映。"""
    enabled = await site_config.get_bool(db, "liquidation_enabled")
    hard_thr = await site_config.get_decimal(db, "liquidation_hard_threshold")
    soft_thr = await site_config.get_decimal(db, "liquidation_soft_threshold")
    partial_pct = await site_config.get_decimal(db, "liquidation_partial_pct")
    target_margin = await site_config.get_decimal(db, "liquidation_target_margin")
    emergency_thr = await site_config.get_decimal(db, "liquidation_emergency_threshold")
    interval = await site_config.get_int(db, "liquidation_sweep_interval_sec")
    return {
        "enabled": enabled,
        "hard_threshold": float(hard_thr),
        "soft_threshold": float(soft_thr),
        "partial_pct": float(partial_pct),
        "target_margin": float(target_margin),
        "emergency_threshold": float(emergency_thr),
        "sweep_interval_sec": int(interval),
    }
