"""Liquidation 原子操作。调用方负责事务边界 + 已 lock user。

设计：复用 services.lmsr + services.wealth + services.loan_service，不
重新实现 LMSR 数学。锁顺序遵循 market_locks.py 约定。

注意：用 decrease_debt_locked 直接改 in-memory user 对象，省 1 SELECT FOR UPDATE
+ 1 flush。前提是调用方 (sweep) 已经 lock 了 user。
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
from app.services.lmsr import calculate_lmsr_cost, calculate_lmsr_with_prices, quantize_cost, quantize_price
from app.services.market_locks import lock_outcomes_for_market
from app.services.wealth import _get_sell_fee_rate  # 复用 LCV 统一 fee

_logger = logging.getLogger(__name__)
ZERO = Decimal("0")
ONE = Decimal("1")


async def liquidate_user(
    session: AsyncSession,
    user: User,
    *,
    daily_rate: Decimal,
    trigger_source: str,
    partial_pct: Decimal,
    target_margin: Decimal,
    emergency_threshold: Decimal,
) -> LiquidationEvent:
    """全平 user 持仓 + 最大化还债 + 写 LiquidationEvent。

    Mode 决策 (spec docs/superpowers/specs/2026-05-20-partial-liquidation-design.md):
    - pre_margin < emergency_threshold → mode='emergency'，全平所有 position
    - 否则 → mode='partial'，每 position 按 partial_pct 卖（Task 5 实现）

    注意 Task 4 内 partial 卖出逻辑还没实施，所以传 partial_pct=1.0 让 sell_amount
    退化到 pos.amount (= 全卖 = 兼容原 emergency 行为)。Task 5 才加 partial 分支。

    前提：
    - 调用方已 lock user 行 (SELECT FOR UPDATE)
    - 调用方已在 db.begin() 事务上下文中
    - user.debt > 0

    SSE publish 不在此函数内做——调用方在事务 commit 之后批量推。
    """
    if user.debt <= ZERO:
        raise ValueError("liquidate_user requires user.debt > 0")

    # 0. pre-snapshot 之前先拉 positions + lock outcomes，让 pre_hv 用同一份数据
    #    inline 算（省一次独立的 compute_users_holdings_value 调用 + 其 3-4 个 query）
    pre_cash = user.cash
    pre_debt = user.debt

    # 1. 拉所有持仓（加载 outcome + outcome.market）+ FOR UPDATE 锁 position 行
    pos_res = await session.execute(
        select(Position)
        .options(selectinload(Position.outcome).selectinload(Outcome.market))
        .where(Position.user_id == user.id, Position.amount > 0)
        .order_by(Position.id.asc())
        .with_for_update()
    )
    positions = pos_res.scalars().all()

    # 按 market 分组：同 user 在同 market 多个 outcome 持仓时只锁一次 outcomes
    positions_by_market: dict[int, list[Position]] = {}
    for pos in positions:
        positions_by_market.setdefault(pos.outcome.market_id, []).append(pos)

    # 2. 一次性锁所有相关 market 的 outcomes（按 market_id 升序避死锁），用 lock
    #    后的最新 outcomes 状态算 pre_hv（LCV 口径，等价于 compute_users_holdings_value
    #    但省去 N+1 query；fee_factor 同源避免口径分裂）
    outcomes_by_market: dict[int, list[Outcome]] = {}
    sorted_market_ids = sorted(positions_by_market.keys())
    for market_id in sorted_market_ids:
        market = positions_by_market[market_id][0].outcome.market
        if market.status != MarketStatus.TRADING:
            continue
        outcomes_by_market[market_id] = await lock_outcomes_for_market(session, market_id)

    # 3. inline 算 pre_hv（LCV）：用刚 lock 的 outcomes 数据，不再单独跑
    #    compute_users_holdings_value（省 3-4 个 query）。HALT 市场持仓不计入
    #    （与 services.wealth LCV 函数同语义）。
    fee_factor = ONE - _get_sell_fee_rate()
    pre_hv = ZERO
    for pos in positions:
        market = pos.outcome.market
        if market.status != MarketStatus.TRADING:
            continue
        outs = outcomes_by_market.get(market.id)
        if not outs:
            continue
        idx = next((i for i, o in enumerate(outs) if o.id == pos.outcome_id), None)
        if idx is None:
            continue
        b = float(market.liquidity_b)
        shares_list = [float(o.total_shares) for o in outs]
        old_cost = calculate_lmsr_cost(shares_list, b)
        after = list(shares_list)
        after[idx] -= float(pos.amount)
        new_cost = calculate_lmsr_cost(after, b)
        gross = quantize_cost(old_cost - new_cost)
        pre_hv += quantize_cost(gross * fee_factor)

    pre_nw = pre_cash - pre_debt + pre_hv
    # 注意：本 pre_margin 用 post-lock outcomes 算的 LCV NW，可能跟 sweep
    # stage 2 重算的 margin_now (用 compute_users_holdings_value 不锁 outcomes)
    # 有微小数值差 (~1 LSB)。pre_margin 仅写入 LiquidationEvent 作审计快照，
    # 不参与门槛判定，差异可忽略。详见 review M4。
    # user.debt > 0 已在入口断言，pre_debt 必 > 0，所以 pre_margin 永远是 Decimal
    pre_margin = pre_nw / pre_debt

    # Mode 决策（spec § Mode 决策）
    mode = "emergency" if pre_margin < emergency_threshold else "partial"
    # 注意：target_margin 当前仅 log/audit 输出，不参与判定（spec § sweep tick 传参）。
    # 未来"卖到 margin 达到 target_margin 就停"的优化会读取它；现在 partial 模式
    # 一次只卖 partial_pct 比例，下个 tick 再判。
    _logger.info(
        "liquidate_user_mode",
        extra={
            "user_id": user.id, "mode": mode,
            "pre_margin": float(pre_margin),
            "emergency_threshold": float(emergency_threshold),
            "target_margin": float(target_margin),
        },
    )

    total_proceeds = ZERO
    sold_count = 0

    # 锁顺序说明：market.py BUY/SELL 用 market → outcomes → user，本函数被调用时
    # 调用方已 lock user → 这里 lock outcomes → 顺序 user → outcomes。跟 market.py
    # 的 user 位置不同 → 理论上存在 deadlock 环形。缓解：sweep 层 catch DBAPI
    # DeadlockError + 跳过该用户下一轮。
    # 4. 循环卖出（复用已 lock 的 outcomes_by_market）
    for market_id in sorted_market_ids:
        pos_group = positions_by_market[market_id]
        # 用第一个 position 的 market 引用（同 market_id 共享 market 对象）
        market = pos_group[0].outcome.market
        if market.status != MarketStatus.TRADING:
            for pos in pos_group:
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

        # outcomes 已在阶段 2 lock 过，直接复用
        all_outcomes = outcomes_by_market[market.id]
        b = float(market.liquidity_b)
        outcomes_idx_by_id = {o.id: i for i, o in enumerate(all_outcomes)}

        for pos in pos_group:
            idx = outcomes_idx_by_id.get(pos.outcome_id)
            if idx is None:
                _logger.error(
                    "liquidation_outcome_not_in_market",
                    extra={"user_id": user.id, "position_id": pos.id,
                           "outcome_id": pos.outcome_id, "market_id": market.id},
                )
                continue

            # 算 sell_amount 按 mode（spec § Mode 决策）
            if mode == "emergency":
                sell_amount = pos.amount
            else:  # partial
                sell_amount = (pos.amount * partial_pct).quantize(Decimal("0.000001"))

            if sell_amount <= ZERO:
                # partial 时 amount × pct 量化后为 0 → 跳过本 position（不报错）
                continue

            # partial 算出来 ≥ amount → 退化全卖（边界保护，发生在 LMSR 算 proceeds 之前）
            if sell_amount >= pos.amount:
                sell_amount = pos.amount

            # 每 position 用"最新的 outcomes 状态"算 LMSR：同 market 多 outcome
            # 先后被清算时，shares_list 反映前面已卖的状态（path-independent，总
            # proceeds 不变，但单笔 attribution 跟串行顺序一致）。
            old_q = [float(o.total_shares) for o in all_outcomes]
            new_q = list(old_q)
            new_q[idx] -= float(sell_amount)

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

            # 应用变更：现金 + outcome 份额 + 持仓
            user.cash += proceeds
            all_outcomes[idx].total_shares -= sell_amount

            if sell_amount >= pos.amount:
                # 全卖 (emergency 或 partial 边界)
                await session.delete(pos)
            else:
                # partial 卖：用"真实卖出比例" sell_amount/pos.amount 减 cost_basis，
                # 严格保持 avg_price = cost_basis / amount 不变 (review I-3 修；
                # 之前用 partial_pct 在 sell_amount quantize 后会引入 sub-LSB 漂移)。
                cost_reduced = (pos.cost_basis * sell_amount / pos.amount).quantize(Decimal("0.000001"))
                pos.amount -= sell_amount
                pos.cost_basis -= cost_reduced

            # 记 LIQUIDATE Transaction（与 SELL 格式一致；按 sell_amount 算）
            avg_price = (
                quantize_price(proceeds / sell_amount) if sell_amount > ZERO else ZERO
            )
            tx = Transaction(
                user_id=user.id,
                outcome_id=pos.outcome_id,
                type=TransactionType.LIQUIDATE,
                shares=sell_amount,
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

    # 2. 最大化还债：用 decrease_debt_locked 直接改 in-memory user 对象，
    #    省 1 SELECT FOR UPDATE + 1 flush（旧版要 flush 让 cash 落库才能让
    #    decrease_debt 内部 SELECT 读到，新版直接操作已 lock 的 user 对象）。
    repaid = ZERO
    if user.cash > ZERO and user.debt > ZERO:
        repay_amount = min(user.cash, user.debt).quantize(Decimal("0.000001"))
        if repay_amount > ZERO:
            repaid = await loan_service.decrease_debt_locked(
                session, user, repay_amount,
                consume_cash=True, daily_rate=daily_rate,
            )
            # decrease_debt_locked 已直接更新 user 对象，无需手动同步

    # 仅当实际卖出了仓位或还了债时才更新时间戳 / 写 event；
    # 全部仓位 proceeds<0 而跳过的"卡水下"用户不算真正被强平，
    # 避免 UI 显示"刚被强平"误导用户，也减少噪声 event 行。
    if sold_count == 0 and repaid == ZERO:
        _logger.info(
            "liquidation_noop_skipped",
            extra={
                "user_id": user.id,
                "trigger_source": trigger_source,
                "reason": "sold_count=0 and repaid=0; last_liquidated_at and event not written",
            },
        )
        # 构造一个 noop event 对象供调用方检查计数，但不写入 DB
        return LiquidationEvent(
            user_id=user.id,
            triggered_at=datetime.now(timezone.utc),
            pre_cash=pre_cash,
            pre_debt=pre_debt,
            pre_holdings_value=pre_hv,
            pre_net_worth=pre_nw,
            pre_margin_ratio=pre_margin,
            sold_positions_count=0,
            total_proceeds=ZERO,
            repaid_amount=ZERO,
            remaining_debt=user.debt,
            post_cash=user.cash,
            trigger_source=trigger_source,
            mode=mode,
        )

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
        mode=mode,
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
