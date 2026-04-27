"""Loan 玩家接口。所有 handler 为 loan_service 薄封装。"""
from __future__ import annotations
import logging
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.database import get_async_session
from app.core.users import current_active_user
from app.models.base import User, Position, Outcome, Market, MarketStatus
from app.schemas.loan import LoanQuotaResponse, BorrowRequest, LoanActionResponse, RepayRequest
from app.services import site_config, loan_service
from app.services.lmsr import get_current_price

router = APIRouter()
logger = logging.getLogger("thccb.loan")


async def _holdings_value(db: AsyncSession, user_id: int) -> Decimal:
    """用 LMSR 瞬时价估算持仓价值（TRADING 状态的市场）。"""
    pos_result = await db.execute(select(Position).where(Position.user_id == user_id))
    positions = list(pos_result.scalars().all())
    total = Decimal("0")
    for p in positions:
        if p.amount <= 0:
            continue
        o = await db.get(Outcome, p.outcome_id)
        if not o:
            continue
        m = await db.get(Market, o.market_id)
        if not m or m.status != MarketStatus.TRADING:
            continue
        outs_result = await db.execute(select(Outcome).where(Outcome.market_id == m.id))
        outs = list(outs_result.scalars().all())
        idx = next((i for i, x in enumerate(outs) if x.id == o.id), None)
        if idx is None:
            continue
        shares = [float(x.total_shares) for x in outs]
        price = Decimal(str(get_current_price(shares, idx, m.liquidity_b)))
        total += (p.amount * price).quantize(Decimal("0.000001"))
    return total


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
    # 实际扣减由 effective 字段返回，前端可展示"实际还款 ¥N"。
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
