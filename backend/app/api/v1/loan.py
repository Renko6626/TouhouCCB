"""Loan 玩家接口。所有 handler 为 loan_service 薄封装。"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.database import get_async_session
from app.core.users import current_active_user
from app.services.credit.cash import available_cash
from app.models.base import User, LiquidationEvent
from app.models.fx import FxPair
from app.models.title import Title as _Title
from app.services.credit.account_read import account_risk_fields, build_short_positions, borrow_blocked_reason
from app.schemas.loan import LoanQuotaResponse, BorrowRequest, LoanActionResponse, RepayRequest
from app.services import site_config, loan_service
from app.services.wealth import compute_users_holdings_value
from app.services.market_locks import lock_user
from app.services.credit import flags as credit_flags
from app.services.credit.gates import GATES
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.risk import discover_dependencies, check_new_risk, PostTradeState
from app.services.credit.valuation import value_user_detailed
from app.services.credit.version import economic_version_of

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



def _require_writes():
    flags = credit_flags.get_flags()
    if (flags.unified_credit_enabled or flags.read_only_instance
            or credit_flags.read_only_from_env() or OWNERSHIP.reason is not None):
        OWNERSHIP.require_writes()


async def _unified_quota(db: AsyncSession, user_id: int):
    rate = await site_config.get_decimal(db, "loan_daily_rate")
    valuation = await value_user_detailed(db, user_id, daily_rate=rate)
    thresholds = credit_flags.get_flags().thresholds
    user = (await db.execute(select(User).where(User.id == user_id)
                            .execution_options(populate_existing=True))).scalar_one()
    await credit_flags.refresh_new_risk_frozen(db)
    enabled = await site_config.get_bool(db, "loan_enabled")
    reason = borrow_blocked_reason(valuation, thresholds, loan_enabled=enabled,
        credit_frozen=user.credit_frozen, new_risk_frozen=credit_flags.new_risk_frozen())
    # 整组正资产 A 与空头回补成本 K 与 valuation 同源；K 未知（或现金用途不变量
    # 被破坏）时 liquidation_equity 为 None，不得折算成 0 或抛 500（spec §5.2）。
    positive_assets = sum(
        (group.value for group in valuation.groups
         if group.role == "asset_sale" and group.value is not None),
        Decimal("0"),
    )
    cover = valuation.short_cover_cost
    equity = valuation.liquidation_equity
    if reason is not None:
        max_borrow = Decimal("0")
    else:
        # 共享空头公式 max(0, (L-1)E - D - αK)；绝不退回金债-only max_borrow。
        max_borrow = thresholds.max_new_gold_loan(
            equity=equity, debt=valuation.debt_effective,
            positive_assets=positive_assets, short_cover=cover,
        )
    return LoanQuotaResponse(
        enabled=enabled, credit_frozen=user.credit_frozen,
        new_risk_frozen=credit_flags.new_risk_frozen(), borrow_blocked_reason=reason,
        cash=valuation.cash, debt=valuation.debt_effective,
        net_worth=valuation.liquidation_equity,
        leverage_k=thresholds.leverage - Decimal("1"), daily_rate=rate,
        max_borrow=max_borrow,
        last_accrued_at=user.debt_last_accrued_at,
        display_equity=valuation.display_equity,
        liquidation_equity=valuation.liquidation_equity,
        r_initial=thresholds.r_initial, r_maintenance=thresholds.r_maintenance,
        **account_risk_fields(valuation, thresholds),
        short_positions=build_short_positions(valuation,
            fx_enabled=await site_config.get_bool_or(db, "fx_enabled", False),
            unified_enabled=credit_flags.get_flags().unified_credit_enabled),
    )


async def _borrow_unified(db: AsyncSession, user_id: int, amount: Decimal):
    flags = credit_flags.get_flags()
    for attempt in range(flags.credit_risk_retry_limit + 1):
        deps = await discover_dependencies(db, user_id)
        await db.rollback()  # No connection/transaction held while waiting for gates.
        async with GATES.hold(shared=deps.groups):
            try:
                user = await lock_user(db, user_id)
                if economic_version_of(user) != deps.economic_version:
                    await db.rollback()
                    continue
                now = datetime.now(timezone.utc)
                effective = loan_service.pending_debt(user, deps.daily_rate, now)
                # Borrowed principal starts accruing now, not at the previous
                # debt accrual timestamp. Only the old principal earns pending interest.
                decision = await check_new_risk(
                    db, user=user, deps=replace(deps, debt_last_accrued_at=now),
                    post=PostTradeState(cash=Decimal(user.cash) + amount,
                                        debt=effective + amount),
                    thresholds=flags.thresholds, partial_pct=Decimal("1"), now=now,
                )
                if not decision.allowed:
                    if decision.reason == "version_conflict":
                        await db.rollback()
                        continue
                    raise HTTPException(status_code=400, detail=decision.reason)
                u = await loan_service.increase_debt(
                    db, user_id, amount, grant_cash=True, daily_rate=deps.daily_rate,
                    source="borrow", operator_user_id=None, now=now,
                )
                response = LoanActionResponse(
                    cash=u.cash, debt=u.debt, max_borrow=decision.max_borrow,
                )
                _require_writes()
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return response
    raise HTTPException(status_code=409, detail="version_conflict; retry")


@router.get("/quota", response_model=LoanQuotaResponse)
async def get_quota(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    if credit_flags.get_flags().unified_credit_enabled:
        return await _unified_quota(db, int(user.id))
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
    _require_writes()
    enabled = await site_config.get_bool(db, "loan_enabled")
    if not enabled:
        raise HTTPException(status_code=403, detail="借款功能已关闭")

    if credit_flags.get_flags().unified_credit_enabled:
        return await _borrow_unified(db, int(user.id), Decimal(req.amount))

    k = await site_config.get_decimal(db, "loan_leverage_k")
    rate = await site_config.get_decimal(db, "loan_daily_rate")
    amount = Decimal(req.amount)

    # 额度校验必须在 user 行锁之下算：否则并发多笔 borrow 各自用同一份未加锁快照
    # 过检，叠加后远超额度（核心审计 2026-08-22 #1）。同一 session 内 increase_debt
    # 再次 FOR UPDATE 是同事务重入，不会阻塞。
    locked = await lock_user(db, user.id)
    hv = await _holdings_value(db, user.id)
    max_borrow = loan_service.compute_max_borrow(locked, hv, k)
    if amount > max_borrow:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"借款额超出额度（可借 {max_borrow}，申请 {amount}）",
        )

    u = await loan_service.increase_debt(
        db, user.id, amount, grant_cash=True, daily_rate=rate,
        source="borrow", operator_user_id=None,
    )
    response = LoanActionResponse(
        cash=u.cash, debt=u.debt,
        max_borrow=loan_service.compute_max_borrow(u, hv, k),
    )
    user_id = int(u.id)
    _require_writes()
    await db.commit()
    logger.info(
        "LOAN_BORROW user_id=%s amount=%s new_cash=%s new_debt=%s",
        user_id, amount, response.cash, response.debt,
    )
    return response


@router.post("/repay", response_model=LoanActionResponse)
async def repay(
    req: RepayRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    return await _repay(user, db, Decimal(req.amount))


@router.post("/repay-all", response_model=LoanActionResponse)
async def repay_all(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    """锁内结息后按最新债务与现金还到上限，不使用页面查询时的金额。"""
    return await _repay(user, db, None)


async def _repay(user: User, db: AsyncSession, amount: Decimal | None):
    _require_writes()
    rate = await site_config.get_decimal(db, "loan_daily_rate")

    # 不预检金额上限：服务层在锁内结息后按真实 debt/cash 封顶；None 表示还到上限。
    # 这样：(1) 不会因复利让 cash 跑负 (2) 用户输入超额（>debt 或 >cash）会被静默封顶，
    # 实际扣减由 effective 字段返回，前端可展示"实际还款 金 N"。
    # 现金预检也使用锁内最新值，避免另一个会话的转账/成交使页面快照过期。
    locked = await lock_user(db, user.id)
    if await available_cash(db, locked) <= 0 and locked.debt > 0:
        await db.rollback()
        raise HTTPException(status_code=400, detail="现金为 0，无法还款；请先卖出持仓变现")

    try:
        u, effective = await loan_service.decrease_debt(
            db, user.id, amount, consume_cash=True, daily_rate=rate,
            source="repay", operator_user_id=None,
        )
    except (ValueError, loan_service.LoanServiceError) as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    response = LoanActionResponse(
        cash=u.cash, debt=u.debt, max_borrow=None, effective=effective,
    )
    user_id = int(u.id)
    _require_writes()
    await db.commit()
    logger.info(
        "LOAN_REPAY user_id=%s requested=%s effective=%s new_cash=%s new_debt=%s",
        user_id, amount, effective, response.cash, response.debt,
    )
    return response


@router.get("/recent-liquidations", summary="最近强平记录（公开，首页展示）")
async def recent_liquidations(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_session),
):
    """匿名可访问。教育警示用。"""
    stmt = (
        select(LiquidationEvent, User.id, User.username, _Title)
        .join(User, User.id == LiquidationEvent.user_id)
        .outerjoin(_Title, _Title.id == User.equipped_title_id)
        .order_by(LiquidationEvent.triggered_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "id": int(ev.id),
            "user_id": int(user_id),
            "username": username,
            "equipped_title": (
                {"id": t.id, "name": t.name, "color": t.color, "icon": t.icon}
                if t is not None else None
            ),
            "triggered_at": (
                ev.triggered_at.replace(tzinfo=timezone.utc)
                if ev.triggered_at.tzinfo is None else ev.triggered_at
            ).isoformat(),
            "pre_cash": float(ev.pre_cash),
            "pre_debt": float(ev.pre_debt),
            "pre_holdings_value": float(ev.pre_holdings_value) if ev.pre_holdings_value is not None else None,
            "pre_net_worth": float(ev.pre_net_worth) if ev.pre_net_worth is not None else None,
            "pre_margin_ratio": float(ev.pre_margin_ratio)
                if ev.pre_margin_ratio is not None else None,
            "sold_positions_count": int(ev.sold_positions_count),
            "total_proceeds": float(ev.total_proceeds),
            "repaid_amount": float(ev.repaid_amount),
            "remaining_debt": float(ev.remaining_debt),
            "post_cash": float(ev.post_cash),
            "fully_liquidated": ev.remaining_debt == Decimal("0"),
            "trigger_source": ev.trigger_source,
            "mode": ev.mode,
            "product": ev.product,
        }
        for ev, user_id, username, t in rows
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
    legacy = {
        "enabled": enabled,
        "hard_threshold": float(hard_thr),
        "soft_threshold": float(soft_thr),
        "partial_pct": float(partial_pct),
        "target_margin": float(target_margin),
        "emergency_threshold": float(emergency_thr),
        "sweep_interval_sec": int(interval),
    }

    flags = credit_flags.get_flags()
    thresholds = flags.thresholds
    rates = (await db.execute(select(FxPair.id, FxPair.currency_code, FxPair.sell_fee_rate)
                              .where(FxPair.status != "draft").order_by(FxPair.id))).all()
    return {
        **legacy,
        "partial_pct": float(partial_pct),
        "unified_credit_enabled": flags.unified_credit_enabled,
        "credit_leverage": float(thresholds.leverage) if thresholds else None,
        "r_initial": float(thresholds.r_initial) if thresholds else None,
        "r_maintenance": float(thresholds.r_maintenance) if thresholds else None,
        "sell_fee_rate": float(await site_config.get_decimal_or(db, "sell_fee_rate", Decimal("0"))),
        "fx_sell_fee_rates": [{"pair_id": pid, "currency_code": code, "sell_fee_rate": float(rate)}
                              for pid, code, rate in rates],
        "legacy": {"legacy": True, **{k: legacy[k] for k in (
            "hard_threshold", "soft_threshold", "target_margin", "emergency_threshold", "partial_pct")}},
    }
