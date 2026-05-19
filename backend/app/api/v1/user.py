# app/api/v1/user.py

from __future__ import annotations

import logging
from decimal import Decimal
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_async_session, managed_transaction
from app.core.users import current_active_user, current_superuser
from app.models.base import User, Position, Transaction, Outcome, Market
from app.schemas.user import HoldingRead, UserSummary, TransactionRead
from app.services.lmsr import calculate_lmsr_cost, get_current_price, quantize_cost, quantize_price
from app.api.v1.market import SELL_FEE_RATE
from app.services import site_config as _site_config, loan_service as _loan_service
from app.services.rank import rank_title
from app.schemas.loan import ForceLoanRequest, ForgiveDebtRequest
from app.services.wealth import compute_users_holdings_value, compute_users_holdings_value_mtm

_loan_admin_logger = logging.getLogger("thccb.loan_admin")

logger = logging.getLogger(__name__)

router = APIRouter()

ZERO = Decimal("0")
ONE = Decimal("1")


# 称号统一走 app.services.rank.rank_title（全站一套阈值/文案）


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

    total_cost_basis = ZERO
    for pos in positions:
        total_cost_basis += pos.cost_basis

    # 双口径估值：MTM (主显示) + LCV (强平/借款保守口径)
    # 详见 docs/holdings-value-semantics.md
    holdings_mtm = (
        await compute_users_holdings_value_mtm(db, user_ids=[user.id])
    ).get(user.id, ZERO)
    holdings_lcv = (
        await compute_users_holdings_value(db, user_ids=[user.id])
    ).get(user.id, ZERO)

    # net_worth (主显示/排名)用 MTM；net_worth_liquidation 用 LCV (margin 用)
    net_worth = user.cash - user.debt + holdings_mtm
    net_worth_lcv = user.cash - user.debt + holdings_lcv
    unrealized_pnl = holdings_mtm - total_cost_basis        # MTM 主显示
    unrealized_pnl_lcv = holdings_lcv - total_cost_basis    # LCV 保守口径
    rank = rank_title(net_worth)

    # 阈值统一作为公共字段返回（不论 debt 是否 > 0），让前端可动态显示
    # site_config 缺失时 fallback 默认值，跟 sweep / liquidate 用同一套约定
    try:
        hard = await _site_config.get_decimal(db, "liquidation_hard_threshold")
        soft = await _site_config.get_decimal(db, "liquidation_soft_threshold")
    except Exception:
        hard, soft = Decimal("0.2"), Decimal("0.5")

    # Margin classification — 用保守 LCV 口径，避免大仓位用户被 MTM 估值误导成"安全"
    margin_ratio = None
    margin_status = "healthy"
    if user.debt > ZERO:
        margin_ratio = (net_worth_lcv / user.debt).quantize(Decimal("0.000001"))
        if margin_ratio < hard:
            margin_status = "danger"
        elif margin_ratio < soft:
            margin_status = "warning"

    return {
        "cash": user.cash.quantize(Decimal("0.01")),
        "debt": user.debt.quantize(Decimal("0.01")),
        "holdings_value": holdings_mtm.quantize(Decimal("0.01")),
        "holdings_value_liquidation": holdings_lcv.quantize(Decimal("0.01")),
        "total_cost_basis": total_cost_basis.quantize(Decimal("0.01")),
        "unrealized_pnl": unrealized_pnl.quantize(Decimal("0.01")),
        "unrealized_pnl_liquidation": unrealized_pnl_lcv.quantize(Decimal("0.01")),
        "net_worth": net_worth.quantize(Decimal("0.01")),
        "net_worth_liquidation": net_worth_lcv.quantize(Decimal("0.01")),
        "rank": rank,
        "margin_ratio": margin_ratio,
        "margin_status": margin_status,
        "last_liquidated_at": user.last_liquidated_at,
        "margin_hard_threshold": hard.quantize(Decimal("0.0001")),
        "margin_soft_threshold": soft.quantize(Decimal("0.0001")),
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

        # 双口径浮盈：MTM 主（账面）+ LCV 副（立即变现），跟 /user/summary 同款双口径策略
        mtm_value = (pos.amount * price_d).quantize(Decimal("0.000001"))
        unrealized_pnl_mtm = mtm_value - pos.cost_basis
        unrealized_pnl_lcv = market_value - pos.cost_basis

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
                unrealized_pnl=unrealized_pnl_mtm.quantize(Decimal("0.01")),
                unrealized_pnl_liquidation=unrealized_pnl_lcv.quantize(Decimal("0.01")),
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


# ── 用户自助修改昵称 ─────────────────────────
# username 在本系统中只是显示名（登录走 Casdoor SSO 关联 casdoor_id），改动不影响
# 登录。历史成交记录里的 username 是发送时刻的快照，不回填——与 Twitter / Discord
# 等标准做法一致。
import re

_USERNAME_RE = re.compile(r"^[\w一-龥\-]{2,32}$")


class UpdateUsernameRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)


@router.patch("/me/username", summary="修改我的昵称")
async def update_my_username(
    req: UpdateUsernameRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    new_name = req.username.strip()
    if not _USERNAME_RE.match(new_name):
        raise HTTPException(
            status_code=400,
            detail="昵称仅支持中文/英文/数字/下划线/连字符，长度 2-32 字符",
        )
    if new_name == user.username:
        return {"username": user.username, "changed": False}

    async with managed_transaction(db):
        # 行锁自己，避免并发改名导致最终落不到唯一索引（虽然 DB 也会兜底）
        locked = (await db.execute(
            select(User).where(User.id == user.id).with_for_update()
        )).scalar_one()

        # unique 校验：另一行用了这个 username？
        dup = (await db.execute(
            select(User.id).where(User.username == new_name).where(User.id != user.id)
        )).first()
        if dup:
            raise HTTPException(status_code=409, detail="该昵称已被占用")

        old_name = locked.username
        locked.username = new_name

    logger.info(
        "USERNAME_UPDATE user_id=%s old=%s new=%s",
        user.id, old_name, new_name,
    )
    return {"username": new_name, "changed": True}


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


# ── 管理员之间互相授权 ──────────────────────
# 第一个注册的用户在 auth.py 中自动 is_superuser=True；之后管理员可以通过本接口
# 提升/取消他人管理员权限。安全围栏：
# 1. 不能改自己（避免自我降级锁死）
# 2. 不能取消"最后一个"管理员（否则全站没人能管）
# 3. 目标用户必须存在（404）

class SetAdminRequest(BaseModel):
    is_admin: bool = Field(..., description="True=提升为管理员；False=取消管理员")


@router.patch("/{user_id}/admin", summary="设置/取消用户的管理员权限（仅管理员）")
async def set_user_admin(
    user_id: int,
    req: SetAdminRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="不能修改自己的管理员权限")

    async with managed_transaction(db):
        target = (await db.execute(
            select(User).where(User.id == user_id).with_for_update()
        )).scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=404, detail="用户不存在")

        if target.is_superuser == req.is_admin:
            return {
                "user_id": user_id,
                "username": target.username,
                "is_admin": target.is_superuser,
                "changed": False,
            }

        # 取消管理员前先确认不是最后一个
        if not req.is_admin:
            count_stmt = select(func.count()).select_from(User).where(User.is_superuser == True)  # noqa: E712
            admin_count = int((await db.execute(count_stmt)).scalar_one())
            if admin_count <= 1:
                raise HTTPException(status_code=400, detail="不能取消最后一个管理员")

        target.is_superuser = req.is_admin

    logger.info(
        "SET_ADMIN by_admin_id=%s target_user_id=%s target_username=%s new_is_admin=%s",
        admin.id, user_id, target.username, req.is_admin,
    )
    return {
        "user_id": user_id,
        "username": target.username,
        "is_admin": target.is_superuser,
        "changed": True,
    }


# ── 批量调整现金（仅管理员） ─────────────────
# 安全围栏：
# 1. dry_run 必须先调一次拿到匹配预览，前端二次确认后再 dry_run=false 执行
# 2. 一次最多影响 BATCH_HARDCAP 个用户，超过 → 400（防止 filter 不严误伤）
# 3. 任一用户操作后 cash<0 → 该用户跳过（记 failed），不影响其他用户
# 4. 单事务 + FOR UPDATE 按 id ASC 锁，防止跨用户死锁
# 5. amount=0 → 400（无意义；且若允许会把"什么都不做"算成功，混淆审计）
BATCH_ADJUST_HARDCAP = 500


class BatchAdjustCashFilter(BaseModel):
    user_id_min: Optional[int] = Field(default=None, description="包含")
    user_id_max: Optional[int] = Field(default=None, description="包含")
    cash_min: Optional[Decimal] = Field(default=None)
    cash_max: Optional[Decimal] = Field(default=None)
    debt_min: Optional[Decimal] = Field(default=None)
    debt_max: Optional[Decimal] = Field(default=None)
    is_active: Optional[bool] = Field(default=None, description="None=不过滤")
    include_superuser: bool = Field(default=False, description="默认排除超管，避免误改自己")


class BatchAdjustCashRequest(BaseModel):
    filter: BatchAdjustCashFilter
    amount: Decimal = Field(..., description="正数加钱，负数扣钱；不能为 0")
    reason: str = Field(..., min_length=1, max_length=200, description="审计原因，必填")
    dry_run: bool = Field(default=True, description="默认 true 只预览不写入；false 才实际执行")


def _build_user_filter_stmt(f: BatchAdjustCashFilter):
    """根据 filter 构建 select(User) 语句，复用给 dry_run 预览和实际执行。"""
    stmt = select(User)
    conds = []
    if f.user_id_min is not None:
        conds.append(User.id >= f.user_id_min)
    if f.user_id_max is not None:
        conds.append(User.id <= f.user_id_max)
    if f.cash_min is not None:
        conds.append(User.cash >= f.cash_min)
    if f.cash_max is not None:
        conds.append(User.cash <= f.cash_max)
    if f.debt_min is not None:
        conds.append(User.debt >= f.debt_min)
    if f.debt_max is not None:
        conds.append(User.debt <= f.debt_max)
    if f.is_active is not None:
        conds.append(User.is_active == f.is_active)
    if not f.include_superuser:
        conds.append(User.is_superuser == False)  # noqa: E712 SQLAlchemy 要用 == 不是 is
    for c in conds:
        stmt = stmt.where(c)
    return stmt.order_by(User.id.asc())  # 升序，事务内锁顺序一致防死锁


@router.post("/batch-adjust-cash", summary="批量调整用户现金（仅管理员）")
async def batch_adjust_user_cash(
    req: BatchAdjustCashRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    if req.amount == 0:
        raise HTTPException(status_code=400, detail="amount 不能为 0")

    # 先用普通 select 预览匹配范围 + 拿计数做 hardcap 检查
    preview_stmt = _build_user_filter_stmt(req.filter)
    preview = (await db.execute(preview_stmt)).scalars().all()
    matched_count = len(preview)
    if matched_count > BATCH_ADJUST_HARDCAP:
        raise HTTPException(
            status_code=400,
            detail=f"匹配用户数 {matched_count} 超过单批上限 {BATCH_ADJUST_HARDCAP}，请收紧 filter",
        )

    matched_users = [
        {
            "id": u.id,
            "username": u.username,
            "cash_before": float(u.cash.quantize(Decimal("0.01"))),
            "debt": float(u.debt.quantize(Decimal("0.01"))),
            "cash_after": float((u.cash + req.amount).quantize(Decimal("0.01"))),
            "will_fail": (u.cash + req.amount) < 0,
        }
        for u in preview
    ]
    will_fail_count = sum(1 for m in matched_users if m["will_fail"])
    eligible_count = matched_count - will_fail_count
    total_delta = float((Decimal(eligible_count) * req.amount).quantize(Decimal("0.01")))

    if req.dry_run:
        return {
            "dry_run": True,
            "matched_count": matched_count,
            "eligible_count": eligible_count,
            "will_fail_count": will_fail_count,
            "total_delta": total_delta,
            "matched_users": matched_users,
        }

    # 实际执行：单事务内 FOR UPDATE 重新锁全部匹配的行（按 id ASC 防死锁）
    updated: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    async with managed_transaction(db):
        locked_stmt = _build_user_filter_stmt(req.filter).with_for_update()
        users = (await db.execute(locked_stmt)).scalars().all()
        # 锁后重新校验数量（极罕见 race：管理员之间并发或用户活动）
        if len(users) > BATCH_ADJUST_HARDCAP:
            raise HTTPException(
                status_code=400,
                detail=f"加锁后匹配 {len(users)} > 上限，操作中止",
            )
        for u in users:
            new_cash = u.cash + req.amount
            if new_cash < 0:
                failed.append({
                    "user_id": u.id,
                    "username": u.username,
                    "reason": "操作后现金为负，已跳过",
                    "cash_before": float(u.cash.quantize(Decimal("0.01"))),
                    "would_be": float(new_cash.quantize(Decimal("0.01"))),
                })
                continue
            u.cash = new_cash
            updated.append({
                "user_id": u.id,
                "username": u.username,
                "cash_before": float((u.cash - req.amount).quantize(Decimal("0.01"))),
                "cash_after": float(new_cash.quantize(Decimal("0.01"))),
            })

    # 审计日志：每个受影响用户单独 log 一行，便于事后筛查
    for row in updated:
        logger.info(
            "BATCH_ADJUST_CASH admin_id=%s user_id=%s amount=%s reason=%s new_cash=%s",
            admin.id, row["user_id"], req.amount, req.reason, row["cash_after"],
        )
    if failed:
        logger.warning(
            "BATCH_ADJUST_CASH admin_id=%s skipped_count=%s amount=%s reason=%s",
            admin.id, len(failed), req.amount, req.reason,
        )

    actual_total_delta = float((Decimal(len(updated)) * req.amount).quantize(Decimal("0.01")))
    return {
        "dry_run": False,
        "updated_count": len(updated),
        "failed_count": len(failed),
        "total_delta": actual_total_delta,
        "updated": updated,
        "failed": failed,
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
    try:
        u = await _loan_service.increase_debt(
            db, user_id, Decimal(req.amount), grant_cash=True, daily_rate=rate,
        )
    except (ValueError, _loan_service.LoanServiceError) as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
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
    try:
        u, effective = await _loan_service.decrease_debt(
            db, user_id, Decimal(req.amount), consume_cash=False, daily_rate=rate,
        )
    except (ValueError, _loan_service.LoanServiceError) as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
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
