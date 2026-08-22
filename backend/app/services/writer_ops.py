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
from sqlalchemy import and_, select, update as sa_update

from app.core.database import async_session_maker
from app.models.base import (
    Market, MarketStatus, Outcome, Position, Transaction, TransactionType, User,
)
from app.schemas.market import SettleResult
from app.services.candle_writer import compute_candle_rows
from app.services.lmsr import calculate_lmsr_with_prices, quantize_cost, quantize_price
from app.services.market_locks import lock_user
from app.services.market_title_gating import assert_user_can_trade_market
from app.services.market_writer import MarketState, MarketWriter, OpOutcome
from app.services.trade_checks import check_buy_slippage, check_sell_slippage
from app.services import site_config
from app.services import audit_service
from app.services.market_open import market_is_open

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
            await audit_service.record_trade(
                session, tx=tx, user=locked_user, position=pos, market_id=state.market_id,
                market_after=audit_service.market_snapshot(
                    outcome_ids=state.outcome_ids, q=new_q_dec, b=b,
                    prices=new_prices, status=state.status),
                extra={"fee_rate": "0", "path": "writer"},
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
    trade_payload = {
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
    }
    return OpOutcome(
        response={
            "shares": float(shares_d),
            "cost": float(pay.quantize(Decimal("0.01"))),
            "new_cash": quantize_cost(new_cash),
            "message": f"成功买入 {shares_d:f} 张 {label}（均价≈{avg_price}）",
        },
        new_q_dec=new_q_dec,
        candle_rows=candle_rows,
        tick_trade=trade_payload,
        publishes=[("trade", {"trade": trade_payload})],
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
            await audit_service.record_trade(
                session, tx=tx, user=locked_user, position=pos, market_id=state.market_id,
                market_after=audit_service.market_snapshot(
                    outcome_ids=state.outcome_ids, q=new_q_dec, b=b,
                    prices=new_prices, status=state.status),
                extra={"fee_rate": sell_fee_rate, "path": "writer"},
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
    trade_payload = {
        "id": int(tx.id), "type": TransactionType.SELL,
        "outcome_id": int(cmd.outcome_id), "username": cmd.username,
        "shares": float(shares_d), "price": float(avg_price),
        "gross": float(proceeds), "fee": float(fee),
        "post_market_price": float(post_mp),
        "market_prices_post": [float(p) for p in new_prices],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return OpOutcome(
        response={
            "shares": float(shares_d),
            "cost": float((-net).quantize(Decimal("0.01"))),
            "new_cash": quantize_cost(new_cash),
            "message": f"卖出成功，获得 {net}（手续费 {fee}，均价≈{avg_price}）",
        },
        new_q_dec=new_q_dec,
        candle_rows=candle_rows,
        tick_trade=trade_payload,
        publishes=[("trade", {"trade": trade_payload})],
    )


@dataclass
class CloseCmd:
    market_id: int
    admin_id: Optional[int] = None


@dataclass
class ResumeCmd:
    market_id: int
    admin_id: Optional[int] = None


async def op_close(state: MarketState, cmd: CloseCmd) -> OpOutcome:
    if state.status == MarketStatus.SETTLED:
        raise HTTPException(status_code=400, detail="市场已结算，无法熔断")
    async with async_session_maker() as session:
        async with session.begin():
            market = await session.get(Market, cmd.market_id)
            market.status = MarketStatus.HALT
            audit_service.record(
                session, "market_close", market_id=cmd.market_id,
                operator_user_id=cmd.admin_id,
                market_after=audit_service.market_snapshot(
                    outcome_ids=state.outcome_ids, q=state.q_dec, b=state.b,
                    prices=state.prices, status=MarketStatus.HALT),
            )
        title = market.title
    return OpOutcome(
        response={"message": f"市场 {title} 已停止交易（熔断）"},
        new_status=MarketStatus.HALT,
        publishes=[("market_status", {"status": MarketStatus.HALT})],
    )


async def op_resume(state: MarketState, cmd: ResumeCmd) -> OpOutcome:
    if state.status == MarketStatus.SETTLED:
        raise HTTPException(status_code=400, detail="市场已结算，无法恢复交易")
    if state.status != MarketStatus.HALT:
        raise HTTPException(status_code=400, detail="市场当前不在熔断状态")
    async with async_session_maker() as session:
        async with session.begin():
            market = await session.get(Market, cmd.market_id)
            market.status = MarketStatus.TRADING
            audit_service.record(
                session, "market_resume", market_id=cmd.market_id,
                operator_user_id=cmd.admin_id,
                market_after=audit_service.market_snapshot(
                    outcome_ids=state.outcome_ids, q=state.q_dec, b=state.b,
                    prices=state.prices, status=MarketStatus.TRADING),
            )
        title = market.title
    return OpOutcome(
        response={"message": f"市场 {title} 已恢复交易"},
        new_status=MarketStatus.TRADING,
        publishes=[("market_status", {"status": MarketStatus.TRADING})],
    )


@dataclass
class ResolveCmd:
    market_id: int
    winning_outcome_id: int
    payout: Decimal
    admin_id: int


async def op_resolve(state: MarketState, cmd: ResolveCmd) -> OpOutcome:
    """移植自 market.py::resolve_market 事务体（结算数学逐字照抄，不重写）。

    market 行不再 `SELECT ... FOR UPDATE`——writer 串行执行本身就是该市场的
    序列化保护（spec § 4.5）。position 行锁保留（防与 sell 等并发路径碰撞，
    零成本照抄）；user 行 `FOR UPDATE` 保留（跨路径 cash 串行化依赖）。
    结算不改变份额总量，故 `new_q_dec=None`。
    """
    payout_unit = quantize_cost(cmd.payout)
    if payout_unit < ZERO:
        raise HTTPException(status_code=422, detail="payout 必须 >= 0")

    async with async_session_maker() as session:
        async with session.begin():
            market = await session.get(Market, cmd.market_id)
            if not market:
                raise HTTPException(status_code=404, detail="市场不存在")

            if market.status == MarketStatus.SETTLED:
                if market.winning_outcome_id is None or market.settled_at is None:
                    raise HTTPException(status_code=500, detail="市场已结算但结算字段缺失（数据异常）")
                return OpOutcome(
                    response=SettleResult(
                        market_id=market.id,
                        status=market.status,
                        winning_outcome_id=int(market.winning_outcome_id),
                        settled_at=market.settled_at,
                        total_payout=ZERO,
                        settled_positions=0,
                    ),
                )

            o_res = await session.execute(
                select(Outcome)
                .where(Outcome.market_id == market.id)
                .order_by(Outcome.id.asc())
            )
            outcomes = o_res.scalars().all()
            if len(outcomes) < 2:
                raise HTTPException(status_code=400, detail="市场选项数量异常")

            winning = next((o for o in outcomes if o.id == cmd.winning_outcome_id), None)
            if not winning:
                raise HTTPException(status_code=400, detail="winning_outcome_id 不属于该市场")

            is_winner = {o.id: (o.id == winning.id) for o in outcomes}

            p_stmt = (
                select(Position)
                .join(Outcome, Position.outcome_id == Outcome.id)
                .where(
                    and_(
                        Outcome.market_id == market.id,
                        Position.amount > 0,
                    )
                )
                .with_for_update()
            )
            p_res = await session.execute(p_stmt)
            positions = p_res.scalars().all()

            # Decimal 计算兑付
            payout_by_user: dict[int, Decimal] = {}
            settled_positions = 0
            lose_txs: list[tuple[Transaction, int]] = []
            now = datetime.now(timezone.utc)

            for pos in positions:
                if pos.amount <= ZERO:
                    continue
                settled_positions += 1

                if is_winner.get(pos.outcome_id, False):
                    payout_amt = pos.amount * payout_unit
                    if payout_amt > ZERO:
                        payout_by_user[pos.user_id] = payout_by_user.get(pos.user_id, ZERO) + payout_amt
                else:
                    # 亏损仓位：记录 settle_lose 交易（图表 shares 重放需要）
                    lose_tx = Transaction(
                        user_id=pos.user_id,
                        outcome_id=pos.outcome_id,
                        type=TransactionType.SETTLE_LOSE,
                        shares=pos.amount,
                        gross=ZERO,
                        fee=ZERO,
                        price=ZERO,
                        pre_market_price=ZERO,
                        post_market_price=ZERO,
                        cost=ZERO,
                        timestamp=now,
                    )
                    session.add(lose_tx)
                    lose_txs.append((lose_tx, pos.user_id))

                await session.delete(pos)

            total_payout = ZERO

            # settle_lose 审计：user 资金不变，仓位归零；快照取当前 user 行
            for lose_tx, lose_uid in lose_txs:
                lu = (await session.execute(
                    select(User).where(User.id == lose_uid).with_for_update())).scalars().first()
                if lu is not None:
                    await audit_service.record_trade(
                        session, tx=lose_tx, user=lu, position=None,
                        market_id=cmd.market_id, market_after=None,
                        extra={"path": "writer"}, operator_user_id=cmd.admin_id,
                    )

            for uid, pay in payout_by_user.items():
                if pay <= ZERO:
                    continue

                u_res = await session.execute(
                    select(User).where(User.id == uid).with_for_update()
                )
                u = u_res.scalars().first()
                if not u:
                    raise HTTPException(status_code=500, detail=f"用户 {uid} 不存在，无法结算（已回滚）")

                u.cash += pay
                total_payout += pay

                win_tx = Transaction(
                    user_id=u.id,
                    outcome_id=winning.id,
                    type=TransactionType.SETTLE,
                    shares=ZERO,
                    gross=pay,
                    fee=ZERO,
                    price=payout_unit,
                    cost=-pay,
                    timestamp=now,
                )
                session.add(win_tx)
                await audit_service.record_trade(
                    session, tx=win_tx, user=u, position=None,
                    market_id=cmd.market_id, market_after=None,
                    extra={"payout_unit": payout_unit, "path": "writer"},
                    operator_user_id=cmd.admin_id,
                )

            for o in outcomes:
                o.payout = payout_unit if o.id == winning.id else ZERO

            market.status = MarketStatus.SETTLED
            market.winning_outcome_id = winning.id
            market.settled_at = now
            market.settled_by_user_id = cmd.admin_id
            audit_service.record(
                session, "market_settle", market_id=cmd.market_id,
                operator_user_id=cmd.admin_id, ts=now,
                payload={"winning_outcome_id": winning.id, "payout_unit": payout_unit,
                         "total_payout": total_payout, "settled_positions": settled_positions,
                         "path": "writer"},
                market_after=audit_service.market_snapshot(
                    outcome_ids=state.outcome_ids, q=state.q_dec, b=state.b,
                    prices=state.prices, status=MarketStatus.SETTLED),
            )
        # ── commit 已成功——事务外读缓存属性（expire_on_commit=False）──
        title = market.title
        winning_id = int(market.winning_outcome_id)
        settled_at = market.settled_at

    logger.info(
        "RESOLVE(writer) market_id=%s winning_outcome_id=%s payout=%s total_payout=%s "
        "settled_positions=%s admin_id=%s",
        cmd.market_id, winning_id, payout_unit, total_payout, settled_positions, cmd.admin_id,
    )

    return OpOutcome(
        response=SettleResult(
            market_id=cmd.market_id,
            status=MarketStatus.SETTLED,
            winning_outcome_id=winning_id,
            settled_at=settled_at,
            total_payout=total_payout,
            settled_positions=int(settled_positions),
        ),
        new_q_dec=None,
        new_status=MarketStatus.SETTLED,
        tick_settlement={"winning_outcome_id": winning_id,
                         "settled_at": settled_at.isoformat()},
        publishes=[("market_status", {
            "status": MarketStatus.SETTLED,
            "winning_outcome_id": winning_id,
            "settled_at": settled_at.isoformat(),
        })],
    )


@dataclass
class LiquidateMarketCmd:
    market_id: int
    user_id: int
    mode: str                 # "emergency" | "partial"
    partial_pct: Decimal
    daily_rate: Decimal = Decimal("0")      # 同事务内立即还债用（核心审计 #3）
    trigger_source: str = "scheduler"


async def op_liquidate_market(state: MarketState, cmd: LiquidateMarketCmd) -> OpOutcome:
    """单市场强平（spec § 4.6）：卖光/按比例卖该 user 在该市场的全部持仓。

    与 op_sell 的关键差异：不检查滑点（强平不受用户设的滑点保护约束），
    不收手续费（与老路径 liquidate_user 同语义）；LIQUIDATE 交易不写 candle
    （现状核实：liquidation_service 从不调 compute_candle_rows，K 线只记 BUY/SELL）。
    """
    from decimal import ROUND_CEILING
    if not market_is_open(state.status, state.closes_at):
        # HALT/SETTLED/已过 closes_at 的市场不强平（用户自己也卖不了），空结果不算错误
        logger.warning(
            "liquidation_skip_non_trading_market(writer) user_id=%s market_id=%s status=%s",
            cmd.user_id, cmd.market_id, state.status,
        )
        return OpOutcome(response={"sold_count": 0, "total_proceeds": ZERO})

    new_q_dec = list(state.q_dec)
    q_work = list(state.q)          # 同市场多仓位串行清算的滚动 q
    total_proceeds = ZERO
    sold_count = 0

    async with async_session_maker() as session:
        async with session.begin():
            locked_user = await lock_user(session, cmd.user_id)
            positions = (await session.execute(
                select(Position)
                .join(Outcome, Position.outcome_id == Outcome.id)
                .where(Position.user_id == cmd.user_id,
                       Position.amount > 0,
                       Outcome.market_id == cmd.market_id)
                .order_by(Position.id.asc())
                .with_for_update()
            )).scalars().all()
            if not positions:
                return OpOutcome(response={"sold_count": 0, "total_proceeds": ZERO})

            for pos in positions:
                idx = _target_idx(state, pos.outcome_id)
                # sell_amount 按 mode（移植 liquidation_service.py:180-196，逐字）
                if cmd.mode == "emergency":
                    sell_amount = pos.amount
                else:
                    sell_amount = (pos.amount * cmd.partial_pct).quantize(
                        Decimal("1"), rounding=ROUND_CEILING)
                if sell_amount <= ZERO:
                    continue
                if sell_amount >= pos.amount:
                    sell_amount = pos.amount

                old_q = list(q_work)
                nq = list(old_q)
                nq[idx] -= float(sell_amount)
                old_cost, old_prices = calculate_lmsr_with_prices(old_q, state.b)
                new_cost, new_prices = calculate_lmsr_with_prices(nq, state.b)
                proceeds = quantize_cost(old_cost - new_cost)
                if proceeds < ZERO:
                    logger.error("liquidation_negative_proceeds(writer) user=%s pos=%s",
                                 cmd.user_id, pos.id)
                    continue    # skip not delete（老路径同语义）

                locked_user.cash += proceeds
                new_q_dec[idx] = quantize_cost(new_q_dec[idx] - sell_amount)
                q_work = nq
                pos_deleted = sell_amount >= pos.amount
                if pos_deleted:
                    await session.delete(pos)
                else:
                    cost_reduced = (pos.cost_basis * sell_amount / pos.amount
                                    ).quantize(Decimal("0.000001"))
                    pos.amount -= sell_amount
                    pos.cost_basis -= cost_reduced

                avg_price = quantize_price(proceeds / sell_amount) if sell_amount > ZERO else ZERO
                liq_tx = Transaction(
                    user_id=cmd.user_id, outcome_id=pos.outcome_id,
                    type=TransactionType.LIQUIDATE, shares=sell_amount,
                    cost=-proceeds, price=avg_price,
                    pre_market_price=quantize_price(old_prices[idx]),
                    post_market_price=quantize_price(new_prices[idx]),
                    gross=proceeds, fee=ZERO,
                    market_prices_post=list(new_prices),
                )
                session.add(liq_tx)
                await audit_service.record_trade(
                    session, tx=liq_tx, user=locked_user,
                    position=None if pos_deleted else pos,
                    market_id=cmd.market_id,
                    market_after=audit_service.market_snapshot(
                        outcome_ids=state.outcome_ids, q=new_q_dec, b=state.b,
                        prices=new_prices, status=state.status),
                    extra={"mode": cmd.mode, "partial_pct": cmd.partial_pct, "path": "writer"},
                )
                total_proceeds += proceeds
                sold_count += 1

            # 镜像批量 SET（每个动过的 outcome 一条 UPDATE）
            for i, oid in enumerate(state.outcome_ids):
                if new_q_dec[i] != state.q_dec[i]:
                    await session.execute(
                        sa_update(Outcome).where(Outcome.id == oid)
                        .values(total_shares=new_q_dec[i]))

            # 回款立即还债（user 行已锁、同事务）：堵住 B→C 之间被花掉的窗口
            repaid = ZERO
            if sold_count and locked_user.cash > ZERO and locked_user.debt > ZERO:
                repay_amount = min(locked_user.cash, locked_user.debt).quantize(Decimal("0.000001"))
                if repay_amount > ZERO:
                    from app.services import loan_service   # 局部 import 避免环
                    debt_before = locked_user.debt
                    repaid = await loan_service.decrease_debt_locked(
                        session, locked_user, repay_amount,
                        consume_cash=True, daily_rate=cmd.daily_rate)
                    audit_service.record_liquidation_repay(
                        session, locked_user, repaid, debt_before, cmd.daily_rate, cmd.trigger_source)

    return OpOutcome(
        response={"sold_count": sold_count, "total_proceeds": total_proceeds, "repaid": repaid},
        new_q_dec=new_q_dec if sold_count else None,
        # 强平今天不发 SSE（与现状一致）
    )


def register_all_ops(writer: MarketWriter) -> None:
    writer.register_op(BuyCmd, op_buy)
    writer.register_op(SellCmd, op_sell)
    writer.register_op(CloseCmd, op_close)
    writer.register_op(ResumeCmd, op_resume)
    writer.register_op(ResolveCmd, op_resolve)
    writer.register_op(LiquidateMarketCmd, op_liquidate_market)
