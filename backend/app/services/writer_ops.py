"""writer 命令实现（spec § 4.3 生命周期）。

每个 op：先内存定价/校验（零 IO，失败即拒），再开独立 DB 事务
（唯一阻塞点），commit 成功后把「新 q / candle 行 / SSE 事件」交给
consumer 统一 apply——op 返回即视为已 commit（spec § 4.4）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, update as sa_update

from app.core.database import async_session_maker
from app.models.base import (
    Market, MarketStatus, Outcome, Position, Transaction, TransactionType, User,
)
from app.services.candle_writer import compute_candle_rows
from app.services.lmsr import calculate_lmsr_with_prices, quantize_cost, quantize_price
from app.services.market_locks import lock_user
from app.services.market_title_gating import assert_user_can_trade_market
from app.services.market_writer import MarketState, MarketWriter, OpOutcome
from app.services.trade_checks import check_buy_slippage, check_sell_slippage
from app.services import site_config

logger = logging.getLogger(__name__)
ZERO = Decimal("0")


def _require_trading_state(state: MarketState) -> None:
    """与 market.py::_require_trading 同语义，输入换成内存 state。"""
    if state.status != MarketStatus.TRADING:
        raise HTTPException(status_code=400, detail="市场当前不可交易")
    if state.closes_at and datetime.now(timezone.utc) >= state.closes_at:
        raise HTTPException(status_code=400, detail="市场已过交易截止时间")


def _target_idx(state: MarketState, outcome_id: int) -> int:
    try:
        return state.outcome_ids.index(int(outcome_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="选项不属于该市场（数据异常）")


@dataclass
class BuyCmd:
    market_id: int
    outcome_id: int
    user_id: int
    username: str
    shares: Decimal
    max_cost: Optional[Decimal]
    max_slippage_bps: Optional[int]
    accept_any_slippage: bool


async def op_buy(state: MarketState, cmd: BuyCmd) -> OpOutcome:
    # ── 1. 内存定价（微秒，零 IO）──
    _require_trading_state(state)
    idx = _target_idx(state, cmd.outcome_id)
    shares_d = quantize_cost(cmd.shares)
    if shares_d <= ZERO:
        raise HTTPException(status_code=422, detail="shares 必须为正数")

    old_q = state.q
    b = state.b
    new_q = list(old_q)
    new_q[idx] += float(shares_d)
    old_cost_f, old_prices = calculate_lmsr_with_prices(old_q, b)
    new_cost_f, new_prices = calculate_lmsr_with_prices(new_q, b)
    pay = quantize_cost(new_cost_f - old_cost_f)
    if pay <= ZERO:
        raise HTTPException(status_code=400, detail="订单异常：成本不应为非正")

    # ── 2. 滑点/校验（纯内存）──
    marginal_price = Decimal(str(old_prices[idx]))
    expected_pay = (marginal_price * shares_d).quantize(Decimal("0.000001"))
    check_buy_slippage(pay, expected_pay, marginal_price,
                       cmd.max_cost, cmd.max_slippage_bps, cmd.accept_any_slippage)

    # 影子新 q（Decimal 6dp 精确加法；commit 后才回写内存）
    new_q_dec = list(state.q_dec)
    new_q_dec[idx] = quantize_cost(new_q_dec[idx] + shares_d)

    avg_price = quantize_price(pay / shares_d)
    pre_mp = quantize_price(old_prices[idx])
    post_mp = quantize_price(new_prices[idx])

    # ── 3. DB 事务（唯一阻塞点）──
    async with async_session_maker() as session:
        async with session.begin():
            locked_user = await lock_user(session, cmd.user_id)
            # title 门槛：与老路径同位置（锁内、扣款前），语义不变
            await assert_user_can_trade_market(session, cmd.user_id, state.market_id)
            if locked_user.cash < pay:
                raise HTTPException(status_code=400, detail="现金不足")
            locked_user.cash -= pay

            pos = (await session.execute(
                select(Position)
                .where(Position.user_id == cmd.user_id,
                       Position.outcome_id == int(cmd.outcome_id))
                .with_for_update()
            )).scalars().first()
            if not pos:
                pos = Position(user_id=cmd.user_id, outcome_id=int(cmd.outcome_id),
                               amount=ZERO, cost_basis=ZERO)
                session.add(pos)
            pos.amount += shares_d
            pos.cost_basis += pay

            tx = Transaction(
                user_id=cmd.user_id,
                outcome_id=int(cmd.outcome_id),
                type=TransactionType.BUY,
                shares=shares_d,
                cost=pay,
                price=avg_price,
                pre_market_price=pre_mp,
                post_market_price=post_mp,
                gross=pay,
                fee=ZERO,
                market_prices_post=list(new_prices),
            )
            session.add(tx)

            # 镜像：writer 是唯一写者，直接 SET 绝对值 = 影子 q_dec（不动点恒等）
            await session.execute(
                sa_update(Outcome)
                .where(Outcome.id == int(cmd.outcome_id))
                .values(total_shares=new_q_dec[idx])
            )
        new_cash = locked_user.cash   # expire_on_commit=False，commit 后可读

    # ── 4. commit 成功 → 组装 apply 数据 ──
    ts = tx.timestamp if tx.timestamp else datetime.now(timezone.utc)
    candle_rows = compute_candle_rows(
        traded_outcome_id=int(cmd.outcome_id),
        outcome_ids=state.outcome_ids,
        pre_prices=old_prices,
        new_prices=new_prices,
        traded_shares=shares_d,
        ts=ts,
    )
    label = state.outcome_labels[idx]
    logger.info(
        "BUY(writer) user_id=%s outcome_id=%s market_id=%s shares=%s cost=%s avg_price=%s "
        "pre_mp=%s post_mp=%s new_cash=%s",
        cmd.user_id, cmd.outcome_id, state.market_id, shares_d, pay, avg_price,
        pre_mp, post_mp, new_cash,
    )
    return OpOutcome(
        response={
            "shares": float(shares_d),
            "cost": float(pay.quantize(Decimal("0.01"))),
            "new_cash": float(new_cash.quantize(Decimal("0.01"))),
            "message": f"成功买入 {shares_d:f} 张 {label}（均价≈{avg_price}）",
        },
        new_q_dec=new_q_dec,
        new_prices=new_prices,
        candle_rows=candle_rows,
        publishes=[(
            "trade",
            {"trade": {
                "id": int(tx.id),
                "type": TransactionType.BUY,
                "outcome_id": int(cmd.outcome_id),
                "username": cmd.username,
                "shares": float(shares_d),
                "price": float(avg_price),
                "gross": float(pay),
                "fee": 0.0,
                "post_market_price": float(post_mp),
                "market_prices_post": [float(p) for p in new_prices],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }},
        )],
    )


@dataclass
class SellCmd:
    market_id: int
    outcome_id: int
    user_id: int
    username: str
    shares: Decimal
    min_proceeds: Optional[Decimal]
    max_slippage_bps: Optional[int]
    accept_any_slippage: bool


async def op_sell(state: MarketState, cmd: SellCmd) -> OpOutcome:
    """与 op_buy 结构对称。

    **与老路径的已知行为差**：老路径先查持仓再算 LMSR；writer 路径滑点/总量
    守卫在内存先算（Step 1，零 IO）、持仓在 DB 事务里查（Step 2）——任何
    「持仓不足 **且**（滑点超限 或 市场总量不足）」的双违规请求，都会先收到
    内存阶段的错误而不是持仓错误。这在单一交易者市场里尤其容易碰到：卖出量
    超过自己全部持仓时，市场总量与持仓数值恒等（持仓 ≤ 总量的不变式），
    「总量不足」会抢在「持仓不足」之前触发。单违规行为完全一致。
    """
    # 1. 内存定价与校验
    _require_trading_state(state)
    idx = _target_idx(state, cmd.outcome_id)
    shares_d = quantize_cost(cmd.shares)
    if shares_d <= ZERO:
        raise HTTPException(status_code=422, detail="shares 必须为正数")
    if state.q[idx] < float(shares_d):
        raise HTTPException(status_code=400, detail="市场总份额不足（异常状态）")

    old_q, b = state.q, state.b
    new_q = list(old_q)
    new_q[idx] -= float(shares_d)
    old_cost_f, old_prices = calculate_lmsr_with_prices(old_q, b)
    new_cost_f, new_prices = calculate_lmsr_with_prices(new_q, b)
    proceeds = quantize_cost(old_cost_f - new_cost_f)
    if proceeds < ZERO:
        proceeds = ZERO

    new_q_dec = list(state.q_dec)
    new_q_dec[idx] = quantize_cost(new_q_dec[idx] - shares_d)

    marginal_price = Decimal(str(old_prices[idx]))
    expected_proceeds = (marginal_price * shares_d).quantize(Decimal("0.000001"))
    avg_price = quantize_price(proceeds / shares_d) if shares_d > ZERO else ZERO
    pre_mp = quantize_price(old_prices[idx])
    post_mp = quantize_price(new_prices[idx])

    # 2. DB 事务：fee 率读取 + 滑点（fee 依赖 site_config，在事务 session 上读，60s 缓存）
    async with async_session_maker() as session:
        async with session.begin():
            sell_fee_rate = await site_config.get_decimal_or(session, "sell_fee_rate", ZERO)
            fee = (proceeds * sell_fee_rate).quantize(Decimal("0.000001"))
            net = proceeds - fee
            check_sell_slippage(proceeds, net, expected_proceeds, marginal_price,
                                cmd.min_proceeds, cmd.max_slippage_bps,
                                cmd.accept_any_slippage)

            locked_user = await lock_user(session, cmd.user_id)
            pos = (await session.execute(
                select(Position)
                .where(Position.user_id == cmd.user_id,
                       Position.outcome_id == int(cmd.outcome_id))
                .with_for_update()
            )).scalars().first()
            if not pos or pos.amount < shares_d:
                raise HTTPException(status_code=400, detail="持仓不足")

            locked_user.cash += net
            if pos.amount > ZERO:
                sold_ratio = shares_d / pos.amount
                pos.cost_basis -= (pos.cost_basis * sold_ratio).quantize(Decimal("0.000001"))
            pos.amount -= shares_d
            if pos.amount <= ZERO:
                pos.cost_basis = ZERO

            tx = Transaction(
                user_id=cmd.user_id, outcome_id=int(cmd.outcome_id),
                type=TransactionType.SELL, shares=shares_d, cost=-net,
                price=avg_price, pre_market_price=pre_mp, post_market_price=post_mp,
                gross=proceeds, fee=fee, market_prices_post=list(new_prices),
            )
            session.add(tx)
            await session.execute(
                sa_update(Outcome).where(Outcome.id == int(cmd.outcome_id))
                .values(total_shares=new_q_dec[idx])
            )
        new_cash = locked_user.cash

    ts = tx.timestamp if tx.timestamp else datetime.now(timezone.utc)
    candle_rows = compute_candle_rows(
        traded_outcome_id=int(cmd.outcome_id), outcome_ids=state.outcome_ids,
        pre_prices=old_prices, new_prices=new_prices, traded_shares=shares_d, ts=ts,
    )
    logger.info(
        "SELL(writer) user_id=%s outcome_id=%s market_id=%s shares=%s proceeds=%s fee=%s "
        "net=%s avg_price=%s new_cash=%s",
        cmd.user_id, cmd.outcome_id, state.market_id, shares_d, proceeds, fee, net,
        avg_price, new_cash,
    )
    return OpOutcome(
        response={
            "shares": float(shares_d),
            "cost": float((-net).quantize(Decimal("0.01"))),
            "new_cash": float(new_cash.quantize(Decimal("0.01"))),
            "message": f"卖出成功，获得 {net}（手续费 {fee}，均价≈{avg_price}）",
        },
        new_q_dec=new_q_dec,
        new_prices=new_prices,
        candle_rows=candle_rows,
        publishes=[(
            "trade",
            {"trade": {
                "id": int(tx.id), "type": TransactionType.SELL,
                "outcome_id": int(cmd.outcome_id), "username": cmd.username,
                "shares": float(shares_d), "price": float(avg_price),
                "gross": float(proceeds), "fee": float(fee),
                "post_market_price": float(post_mp),
                "market_prices_post": [float(p) for p in new_prices],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }},
        )],
    )


def register_all_ops(writer: MarketWriter) -> None:
    writer.register_op(BuyCmd, op_buy)
    writer.register_op(SellCmd, op_sell)
    # Task 8-9 在此追加注册 ResolveCmd / CloseCmd / ResumeCmd / LiquidateMarketCmd
