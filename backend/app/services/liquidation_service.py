"""Liquidation 原子操作。调用方负责事务边界 + 已 lock user。

设计：复用 services.lmsr + services.wealth + services.loan_service，不
重新实现 LMSR 数学。锁顺序遵循 market_locks.py 约定。

注意：decrease_debt 内部会再次 SELECT FOR UPDATE 同一个 user 行；
调用前必须先 flush 让 ORM 将内存中的 cash/debt 变更写入 DB，
否则 decrease_debt 读到的 cash 是 flush 前快照，可能导致扣款金额
与实际现金不符。
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.base import (
    LiquidationEvent, MarketStatus, Outcome, Position,
    Transaction, TransactionType, User,
)
from app.services import loan_service
from app.services.lmsr import calculate_lmsr_with_prices, quantize_cost, quantize_price
from app.services.market_locks import lock_outcomes_for_market
from app.services.wealth import compute_users_holdings_value

_logger = logging.getLogger(__name__)
ZERO = Decimal("0")


async def liquidate_user(
    session: AsyncSession,
    user: User,
    *,
    daily_rate: Decimal,
    trigger_source: str,
) -> LiquidationEvent:
    """全平 user 持仓 + 最大化还债 + 写 LiquidationEvent。

    前提：
    - 调用方已 lock user 行 (SELECT FOR UPDATE)
    - 调用方已在 db.begin() 事务上下文中
    - user.debt > 0

    SSE publish 不在此函数内做——调用方在事务 commit 之后批量推。
    """
    if user.debt <= ZERO:
        raise ValueError("liquidate_user requires user.debt > 0")

    # 0. pre-snapshot（在任何改动之前快照）
    pre_cash = user.cash
    pre_debt = user.debt
    pre_hv = (
        await compute_users_holdings_value(session, user_ids=[user.id])
    ).get(user.id, ZERO)
    pre_nw = pre_cash - pre_debt + pre_hv
    pre_margin = (pre_nw / pre_debt) if pre_debt > ZERO else None

    # 1. 拉所有持仓（加载 outcome + outcome.market，for status check）
    pos_res = await session.execute(
        select(Position)
        .options(selectinload(Position.outcome).selectinload(Outcome.market))
        .where(Position.user_id == user.id, Position.amount > 0)
        .order_by(Position.id.asc())
        .with_for_update()
    )
    positions = pos_res.scalars().all()

    total_proceeds = ZERO
    sold_count = 0

    for pos in positions:
        market = pos.outcome.market
        if market.status != MarketStatus.TRADING:
            _logger.info(
                "liquidation_skip_non_trading_market",
                extra={
                    "user_id": user.id,
                    "position_id": pos.id,
                    "market_id": market.id,
                    "market_status": market.status,
                },
            )
            continue

        # 锁顺序说明：market.py BUY/SELL 用 market → outcomes → user，本函数被调用时
        # 调用方已 lock user，然后我们逐 market 拿 outcomes 锁 → 顺序变成 user → outcomes。
        # 跟 market.py 的 user 位置不同 → 理论上存在 deadlock 环形：
        #   TX1（本函数）: holds user A, wants market M outcomes
        #   TX2（user A 自己的 SELL on M）: holds M+outcomes, wants user A
        # 实际触发条件：sweep 期间用户 A 自己手动/quant 同时 SELL。低概率（10min sweep 间隔）。
        # 缓解策略：在 sweep 层 catch DBAPI DeadlockError + 跳过该用户下一轮处理（见 T6）。
        all_outcomes = await lock_outcomes_for_market(session, market.id)
        idx = next(
            (i for i, o in enumerate(all_outcomes) if o.id == pos.outcome_id), None
        )
        if idx is None:
            _logger.error(
                "liquidation_outcome_not_in_market",
                extra={"user_id": user.id, "position_id": pos.id,
                       "outcome_id": pos.outcome_id, "market_id": market.id},
            )
            continue

        b = float(market.liquidity_b)
        old_q = [float(o.total_shares) for o in all_outcomes]
        new_q = list(old_q)
        new_q[idx] -= float(pos.amount)

        old_cost, old_prices = calculate_lmsr_with_prices(old_q, b)
        new_cost, new_prices = calculate_lmsr_with_prices(new_q, b)
        proceeds = quantize_cost(old_cost - new_cost)

        if proceeds < ZERO:
            _logger.error(
                "liquidation_negative_proceeds",
                extra={
                    "user_id": user.id,
                    "position_id": pos.id,
                    "proceeds": str(proceeds),
                },
            )
            continue  # skip position; spec § 3 says SKIP not DELETE

        # 应用变更：更新用户现金、outcome 份额、删除持仓
        user.cash += proceeds
        all_outcomes[idx].total_shares -= pos.amount
        await session.delete(pos)

        # 记 LIQUIDATE Transaction（与 SELL 格式一致）
        avg_price = (
            quantize_price(proceeds / pos.amount) if pos.amount > ZERO else ZERO
        )
        tx = Transaction(
            user_id=user.id,
            outcome_id=pos.outcome_id,
            type=TransactionType.LIQUIDATE,
            shares=pos.amount,
            cost=-proceeds,         # 与 SELL 同口径：收入为负成本
            price=avg_price,
            pre_market_price=quantize_price(old_prices[idx]),
            post_market_price=quantize_price(new_prices[idx]),
            gross=proceeds,
            fee=ZERO,               # 强平不收手续费
            market_prices_post=list(new_prices),
        )
        session.add(tx)

        total_proceeds += proceeds
        sold_count += 1

    # 2. flush 让 ORM 将 user.cash 写入 DB，之后 decrease_debt 的
    #    SELECT FOR UPDATE 读到的才是含仓位卖出回款后的最新现金
    await session.flush()

    # 3. 最大化还债（decrease_debt 内会再次 SELECT FOR UPDATE user + accrue interest）
    repaid = ZERO
    if user.cash > ZERO and user.debt > ZERO:
        repay_amount = min(user.cash, user.debt).quantize(Decimal("0.000001"))
        if repay_amount > ZERO:
            updated_user, repaid = await loan_service.decrease_debt(
                session, user.id, repay_amount,
                consume_cash=True, daily_rate=daily_rate,
            )
            # 同步 user 对象上的 cash/debt（decrease_debt 改的是它自己 SELECT 出的 ORM 对象）
            user.cash = updated_user.cash
            user.debt = updated_user.debt
            user.debt_last_accrued_at = updated_user.debt_last_accrued_at

    user.last_liquidated_at = datetime.now(timezone.utc)

    # 4. 写 rollup event
    ev = LiquidationEvent(
        user_id=user.id,
        triggered_at=datetime.now(timezone.utc),
        pre_cash=pre_cash,
        pre_debt=pre_debt,
        pre_holdings_value=pre_hv,
        pre_net_worth=pre_nw,
        pre_margin_ratio=pre_margin,
        sold_positions_count=sold_count,
        total_proceeds=total_proceeds,
        repaid_amount=repaid,
        remaining_debt=user.debt,
        post_cash=user.cash,
        trigger_source=trigger_source,
    )
    session.add(ev)
    await session.flush()  # 保证 ev.id 可用

    _logger.warning(
        "user_liquidated",
        extra={
            "user_id": user.id,
            "sold_positions": sold_count,
            "total_proceeds": str(total_proceeds),
            "repaid": str(repaid),
            "remaining_debt": str(user.debt),
            "trigger_source": trigger_source,
        },
    )
    return ev
