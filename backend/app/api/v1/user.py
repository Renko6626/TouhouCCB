# app/api/v1/user.py

from __future__ import annotations

import logging
from decimal import Decimal
from typing import List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_async_session, managed_transaction
from app.core.users import current_active_user, current_superuser
from app.models.base import User, Position, Transaction, Outcome, Market
from app.schemas.user import HoldingRead, UserSummary, TransactionRead
from app.services.lmsr import calculate_lmsr_cost, get_current_price, quantize_cost, quantize_price
from app.api.v1.market import SELL_FEE_RATE
from app.services import site_config as _site_config, loan_service as _loan_service
from app.schemas.loan import ForceLoanRequest, ForgiveDebtRequest

_loan_admin_logger = logging.getLogger("thccb.loan_admin")

logger = logging.getLogger(__name__)

router = APIRouter()

ZERO = Decimal("0")
ONE = Decimal("1")


def _rank_title(net_worth: Decimal) -> str:
    """幻想乡称号"""
    if net_worth > 50000:
        return "大天狗的座上宾"
    if net_worth > 10000:
        return "守矢神社的VIP"
    if net_worth > 2000:
        return "命莲寺的赞助者"
    if net_worth > 500:
        return "人间之里的小商贩"
    return "初入幻想乡的无名氏"


@router.get("/summary", response_model=UserSummary, summary="获取资产概览")
async def get_user_summary(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    pos_stmt = (
        select(Position)
        .where(Position.user_id == user.id, Position.amount > 0)
        .options(selectinload(Position.outcome).selectinload(Outcome.market))
    )
    pos_res = await db.execute(pos_stmt)
    positions: List[Position] = pos_res.scalars().all()

    holdings_value = ZERO
    total_cost_basis = ZERO

    # 批量查出所有相关市场的 outcomes，避免 N+1
    market_ids = list({pos.outcome.market_id for pos in positions})
    outcomes_by_market: Dict[int, List[Outcome]] = {}
    if market_ids:
        all_outcomes_res = await db.execute(
            select(Outcome)
            .where(Outcome.market_id.in_(market_ids))
            .order_by(Outcome.market_id, Outcome.id)
        )
        for o in all_outcomes_res.scalars().all():
            outcomes_by_market.setdefault(o.market_id, []).append(o)

    for pos in positions:
        outcome: Outcome = pos.outcome
        market: Market = outcome.market
        all_outcomes = outcomes_by_market.get(market.id, [])

        shares_list = [float(o.total_shares) for o in all_outcomes]
        idx = next((i for i, o in enumerate(all_outcomes) if o.id == outcome.id), None)
        if idx is None:
            continue

        # 用 LMSR 成本差计算真实清算价值（含滑点），并扣除卖出手续费，与实际可得现金口径一致
        b = float(market.liquidity_b)
        old_cost = calculate_lmsr_cost(shares_list, b)
        after_sell = list(shares_list)
        after_sell[idx] -= float(pos.amount)
        new_cost = calculate_lmsr_cost(after_sell, b)
        gross = quantize_cost(old_cost - new_cost)
        liquidation_value = quantize_cost(gross * (ONE - SELL_FEE_RATE))

        holdings_value += liquidation_value
        total_cost_basis += pos.cost_basis

    net_worth = user.cash - user.debt + holdings_value
    unrealized_pnl = holdings_value - total_cost_basis
    rank = _rank_title(net_worth)

    return {
        "cash": user.cash.quantize(Decimal("0.01")),
        "debt": user.debt.quantize(Decimal("0.01")),
        "holdings_value": holdings_value.quantize(Decimal("0.01")),
        "total_cost_basis": total_cost_basis.quantize(Decimal("0.01")),
        "unrealized_pnl": unrealized_pnl.quantize(Decimal("0.01")),
        "net_worth": net_worth.quantize(Decimal("0.01")),
        "rank": rank,
    }


@router.get("/holdings", response_model=List[HoldingRead], summary="获取持仓明细")
async def get_my_holdings(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    stmt = (
        select(Position)
        .where(Position.user_id == user.id, Position.amount > 0)
        .options(selectinload(Position.outcome).selectinload(Outcome.market))
        .order_by(Position.id.desc())
    )
    res = await db.execute(stmt)
    positions: List[Position] = res.scalars().all()

    # 批量查 outcomes 用于 LMSR 定价
    market_ids = list({pos.outcome.market_id for pos in positions})
    outcomes_by_market: Dict[int, List[Outcome]] = {}
    if market_ids:
        all_outcomes_res = await db.execute(
            select(Outcome)
            .where(Outcome.market_id.in_(market_ids))
            .order_by(Outcome.market_id, Outcome.id)
        )
        for o in all_outcomes_res.scalars().all():
            outcomes_by_market.setdefault(o.market_id, []).append(o)

    results: List[HoldingRead] = []
    for pos in positions:
        outcome: Outcome = pos.outcome
        market: Market = outcome.market
        all_outcomes = outcomes_by_market.get(market.id, [])

        shares_list = [float(o.total_shares) for o in all_outcomes]
        idx = next((i for i, o in enumerate(all_outcomes) if o.id == outcome.id), None)

        if idx is not None:
            b = float(market.liquidity_b)
            price_d = quantize_price(get_current_price(shares_list, idx, b))
            # 真实清算价值 = 卖掉全部持仓 LMSR 实际返还金额（含滑点），并扣除卖出手续费
            old_cost = calculate_lmsr_cost(shares_list, b)
            after_sell = list(shares_list)
            after_sell[idx] -= float(pos.amount)
            new_cost = calculate_lmsr_cost(after_sell, b)
            gross = quantize_cost(old_cost - new_cost)
            market_value = quantize_cost(gross * (ONE - SELL_FEE_RATE))
        else:
            price_d = ZERO
            market_value = ZERO

        avg_price = quantize_price(pos.cost_basis / pos.amount) if pos.amount > ZERO else ZERO

        results.append(
            HoldingRead(
                outcome_id=pos.outcome_id,
                outcome_label=pos.outcome.label,
                market_id=pos.outcome.market_id,
                market_title=pos.outcome.market.title,
                amount=pos.amount.quantize(Decimal("0.01")),
                cost_basis=pos.cost_basis.quantize(Decimal("0.01")),
                avg_price=avg_price,
                current_price=price_d,
                market_value=market_value.quantize(Decimal("0.01")),
                unrealized_pnl=(market_value - pos.cost_basis).quantize(Decimal("0.01")),
            )
        )
    return results


@router.get("/transactions", response_model=List[TransactionRead], summary="获取交易历史")
async def get_my_transactions(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
    limit: int = Query(50, ge=1, le=200, description="最多返回多少条（按时间倒序）"),
):
    """返回最近 N 条交易记录，附带市场/选项名便于前端展示。"""
    stmt = (
        select(Transaction)
        .where(Transaction.user_id == user.id)
        .options(selectinload(Transaction.outcome).selectinload(Outcome.market))
        .order_by(Transaction.timestamp.desc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    txs: List[Transaction] = res.scalars().all()

    results: List[TransactionRead] = []
    for tx in txs:
        outcome = tx.outcome
        market = outcome.market if outcome else None
        results.append(
            TransactionRead(
                id=tx.id,
                outcome_id=tx.outcome_id,
                market_id=market.id if market else None,
                market_title=market.title if market else None,
                outcome_label=outcome.label if outcome else None,
                type=tx.type,
                shares=tx.shares,
                price=tx.price,
                gross=tx.gross,
                fee=tx.fee,
                cost=tx.cost,
                timestamp=tx.timestamp,
            )
        )
    return results


# ==========================================
# 管理员接口
# ==========================================

class AdjustCashRequest(BaseModel):
    amount: Decimal = Field(..., description="正数加钱，负数扣钱")
    reason: str = Field(default="", description="操作原因备注")


@router.post("/{user_id}/adjust-cash", summary="调整用户现金（仅管理员）")
async def adjust_user_cash(
    user_id: int,
    req: AdjustCashRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    async with managed_transaction(db):
        result = await db.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
        user = result.scalars().first()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")

        new_cash = user.cash + req.amount
        if new_cash < 0:
            raise HTTPException(status_code=400, detail=f"操作后现金为 {new_cash}，不能为负")

        user.cash = new_cash

    logger.info(
        "ADJUST_CASH admin_id=%s user_id=%s amount=%s reason=%s new_cash=%s",
        admin.id, user_id, req.amount, req.reason, new_cash,
    )

    return {
        "user_id": user_id,
        "username": user.username,
        "amount": float(req.amount),
        "new_cash": float(new_cash.quantize(Decimal("0.01"))),
        "reason": req.reason,
    }


@router.get("/list", summary="获取用户列表（仅管理员）")
async def list_users(
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    result = await db.execute(
        select(User).order_by(User.id.asc()).limit(200)
    )
    users = result.scalars().all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "cash": float(u.cash.quantize(Decimal("0.01"))),
            "debt": float(u.debt.quantize(Decimal("0.01"))),
            "is_active": u.is_active,
            "is_superuser": u.is_superuser,
        }
        for u in users
    ]


@router.post("/{user_id}/force-loan", summary="管理员强制放贷")
async def force_loan(
    user_id: int,
    req: ForceLoanRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    enabled = await _site_config.get_bool(db, "loan_enabled")
    if not enabled:
        raise HTTPException(status_code=403, detail="借款功能已关闭")
    rate = await _site_config.get_decimal(db, "loan_daily_rate")
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    u = await _loan_service.increase_debt(
        db, user_id, Decimal(req.amount), grant_cash=True, daily_rate=rate,
    )
    await db.commit()
    await db.refresh(u)
    _loan_admin_logger.info(
        "FORCE_LOAN admin_id=%s user_id=%s amount=%s reason=%s new_cash=%s new_debt=%s",
        admin.id, user_id, req.amount, req.reason, u.cash, u.debt,
    )
    return {
        "user_id": user_id,
        "cash": float(u.cash.quantize(Decimal("0.01"))),
        "debt": float(u.debt.quantize(Decimal("0.01"))),
    }


@router.post("/{user_id}/forgive-debt", summary="管理员免除债务")
async def forgive_debt(
    user_id: int,
    req: ForgiveDebtRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    rate = await _site_config.get_decimal(db, "loan_daily_rate")
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    u, effective = await _loan_service.decrease_debt(
        db, user_id, Decimal(req.amount), consume_cash=False, daily_rate=rate,
    )
    await db.commit()
    await db.refresh(u)
    _loan_admin_logger.info(
        "FORGIVE_DEBT admin_id=%s user_id=%s requested=%s effective=%s reason=%s new_debt=%s",
        admin.id, user_id, req.amount, effective, req.reason, u.debt,
    )
    return {
        "user_id": user_id,
        "cash": float(u.cash.quantize(Decimal("0.01"))),
        "debt": float(u.debt.quantize(Decimal("0.01"))),
        "effective": float(effective.quantize(Decimal("0.01"))),
    }
