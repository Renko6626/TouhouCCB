"""writer 命令实现（spec § 4.3 生命周期）。

每个 op：先内存定价/校验（零 IO，失败即拒），再开独立 DB 事务
（唯一阻塞点），commit 成功后把「新 q / candle 行 / SSE 事件」交给
consumer 统一 apply——op 返回即视为已 commit（spec § 4.4）。
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import and_, select, update as sa_update

from app.core.database import async_session_maker
from app.services.credit.cash import available_cash, has_foreign_debt
from app.models.base import (
    Market, MarketStatus, Outcome, Position, Transaction, TransactionType, User,
)
from app.models.credit import LiquidationAction, LiquidationRun
from app.schemas.market import SettleResult
from app.services.candle_writer import compute_candle_rows
from app.services.credit.lmsr_quote import (
    BLOCKED_MARKET_NOT_OPEN,
    BLOCKED_UNKNOWN_OUTCOME,
    OutcomeSnapshot,
    quote_lmsr_group,
)
from app.services.credit.runs import get_or_create_active_run, record_action
from app.services.credit.version import bump_economic_version
from app.services.credit.ownership import OWNERSHIP
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
ONE = Decimal("1")
Q6 = Decimal("0.000001")



# Set only by the consumer/API gate owner. Discovery and retries run with no locks.
CREDIT_DEPS = ContextVar("lmsr_credit_dependencies", default=None)


class CreditRetry(Exception):
    """Release the transaction and every gate, rediscover, and recompute."""


async def check_credit_buy(session, user, market_id, outcome_id, shares, pay, new_q):
    from app.services.credit import flags, risk
    from app.services.credit.keys import GroupKey
    from app.services.credit.gates import GATES
    if not flags.get_flags().unified_credit_enabled:
        return
    # 无金债不再等于无风险（spec §6.3/§11）：锁内按索引确认外币欠币。金债>0 时
    # 短路，不额外查询外币表。乐观首试 deps 为 None 时会 CreditRetry，由外层
    # 释放门闩、重新发现完整依赖后再带 GATES 重试。
    if user.debt <= ZERO and not await has_foreign_debt(session, user.id):
        return
    deps = CREDIT_DEPS.get()
    if deps is None or deps.economic_version != user.economic_version:
        raise CreditRetry()
    target = GroupKey("lmsr", int(market_id))
    if not (set(deps.groups) | {target}) <= GATES.held_keys_by_current_task():
        raise CreditRetry()
    holdings = dict(deps.holdings.get(target, {}))
    holdings[int(outcome_id)] = holdings.get(int(outcome_id), ZERO) + shares
    post = risk.PostTradeState(
        cash=user.cash-pay, debt=user.debt,
        lmsr_q={int(market_id): tuple(new_q)}, post_holdings={target: holdings},
        base_versions={target: deps.snapshots[target].version},
    )
    thresholds = flags.get_flags().thresholds
    if thresholds is None:
        raise HTTPException(503, "credit_configuration_invalid")
    decision = await risk.check_new_risk(
        session, user=user, deps=deps, post=post, thresholds=thresholds,
        partial_pct=Decimal("0.1"), now=datetime.now(timezone.utc),
    )
    if decision.reason == "version_conflict":
        raise CreditRetry()
    if not decision.allowed:
        raise HTTPException(400, decision.reason)


async def repay_sale_proceeds(session, user, net):
    """Record the sale first, then atomically repay its proceeds; no risk admission."""
    from app.services.credit.flags import get_flags
    if not get_flags().unified_credit_enabled or user.debt <= ZERO or net <= ZERO:
        return
    from app.services import loan_service, ledger_service
    rate = await site_config.get_decimal_or(session, "loan_daily_rate", ZERO)
    before = user.debt
    repaid = await loan_service.decrease_debt_locked(
        session, user, net, consume_cash=True, daily_rate=rate)
    if repaid > ZERO:
        await ledger_service.record_entry(
            session, user=user, entry_type="repay", cash_delta=-repaid,
            debt_delta=-repaid, daily_rate=rate, reason="lmsr_sell_proceeds",
            interest_accrued=(user.debt+repaid-before).quantize(Q6))


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
            OWNERSHIP.require_writes()
            # title 门槛：与老路径同位置（锁内、扣款前），语义不变
            await assert_user_can_trade_market(session, cmd.user_id, state.market_id)
            if await available_cash(session, locked_user) < pay:
                raise HTTPException(status_code=400, detail="现金不足")
            await check_credit_buy(session, locked_user, state.market_id, cmd.outcome_id,
                                   shares_d, pay, new_q_dec)
            OWNERSHIP.require_writes()
            locked_user.cash -= pay
            bump_economic_version(locked_user)

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
            "pay": quantize_cost(pay),
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
            OWNERSHIP.require_writes()
            pos = (await session.execute(
                select(Position)
                .where(Position.user_id == cmd.user_id,
                       Position.outcome_id == int(cmd.outcome_id))
                .with_for_update()
            )).scalars().first()
            if not pos or pos.amount < shares_d:
                raise HTTPException(status_code=400, detail="持仓不足")

            OWNERSHIP.require_writes()
            locked_user.cash += net
            bump_economic_version(locked_user)
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
            await repay_sale_proceeds(session, locked_user, net)
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
            "pay": quantize_cost(net),
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
            OWNERSHIP.require_writes()
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
            OWNERSHIP.require_writes()
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
            OWNERSHIP.require_writes()
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
                    payout_amt = quantize_cost(pos.amount * payout_unit)   # 6dp：与 DB 写入、审计快照一致
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

            # 一次性按 user_id 升序锁全部涉及用户（输家 ∪ 赢家）：
            # - 逐用户 FOR UPDATE 是 N 次往返；两市场并发结算时按持仓顺序取锁可互相死锁（审计 L11）
            # - populate_existing：请求 session 里可能已有陈旧 User（管理员自己也持仓）
            all_uids = sorted({int(pos.user_id) for pos in positions})
            users_by_id: dict[int, User] = {}
            if all_uids:
                users_by_id = {int(u.id): u for u in (await session.execute(
                    select(User).where(User.id.in_(all_uids)).order_by(User.id)
                    .with_for_update().execution_options(populate_existing=True)
                )).scalars().all()}

            OWNERSHIP.require_writes()
            for account in users_by_id.values():
                bump_economic_version(account)

            # settle_lose 事件必须在赢家加钱**之前**记：同一用户既输又赢时，
            # settle_lose.user_after.cash 不能含 payout（audit replay 会抓）
            await session.flush()
            for lose_tx, lose_uid in lose_txs:
                lu = users_by_id.get(int(lose_uid))
                if lu is not None:
                    await audit_service.record_trade(
                        session, tx=lose_tx, user=lu, position=None,
                        market_id=cmd.market_id, market_after=None,
                        extra={"path": "writer"}, operator_user_id=cmd.admin_id, flush=False,
                    )

            win_txs: list[tuple[Transaction, User]] = []
            for uid in sorted(payout_by_user):
                pay = payout_by_user[uid]
                if pay <= ZERO:
                    continue
                u = users_by_id.get(int(uid))
                if not u:
                    raise HTTPException(status_code=500, detail=f"用户 {uid} 不存在，无法结算（已回滚）")
                OWNERSHIP.require_writes()
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
                win_txs.append((win_tx, u))

            # 单次 flush 给全部 settle_win 赋 id，再批量追加事件（不逐条 flush，审计 P4）
            await session.flush()
            for win_tx, u in win_txs:
                await audit_service.record_trade(
                    session, tx=win_tx, user=u, position=None,
                    market_id=cmd.market_id, market_after=None,
                    extra={"payout_unit": payout_unit, "path": "writer"},
                    operator_user_id=cmd.admin_id, flush=False,
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


def _require_legacy_liquidation_disabled(entry: str) -> None:
    """WP5：统一执行器开启后，legacy 强平入口必须硬拒绝（避免两个执行者同时卖仓）。

    开关默认 false → 本函数是纯读取，legacy 行为逐字段不变（不查库、不写库）。
    """
    from app.services.credit import flags as credit_flags   # 局部 import 避免环
    if credit_flags.get_flags().unified_credit_enabled:
        raise HTTPException(
            status_code=409,
            detail=f"统一清算已启用（unified_credit_enabled=true），legacy 强平入口 {entry} 已禁用",
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
    不收手续费（legacy 执行器；统一执行器 ``op_liquidate_group`` 按 F5 收普通卖出费）；
    LIQUIDATE 交易不写 candle（现状核实：liquidation_service 从不调 compute_candle_rows，
    K 线只记 BUY/SELL）。

    ``unified_credit_enabled=true`` 时本入口禁用（WP5）：统一执行器接管强平后，
    两个执行者同时卖仓会绕开 ``(run_id, round_no)`` 幂等与组级滚动 q 报价。
    """
    _require_legacy_liquidation_disabled("op_liquidate_market")
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
            OWNERSHIP.require_writes()
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
                bump_economic_version(locked_user)
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
                from app.services import loan_service   # 局部 import 避免环
                # 先结息再算还款额：否则 min(cash, 结息前 debt) 会留下结息增量的灰尘债，
                # 即使现金足够清偿（审计 M3 附带发现）
                debt_before = locked_user.debt
                now = loan_service._compat_now(locked_user)
                loan_service.accrue_interest(locked_user, cmd.daily_rate, now)
                repay_amount = min(locked_user.cash, locked_user.debt).quantize(Decimal("0.000001"))
                if repay_amount > ZERO:
                    repaid = await loan_service.decrease_debt_locked(
                        session, locked_user, repay_amount,
                        consume_cash=True, daily_rate=cmd.daily_rate, now=now)
                    audit_service.record_liquidation_repay(
                        session, locked_user, repaid, debt_before, cmd.daily_rate, cmd.trigger_source)

    return OpOutcome(
        response={"sold_count": sold_count, "total_proceeds": total_proceeds, "repaid": repaid,
                  "debt_after": locked_user.debt},
        new_q_dec=new_q_dec if sold_count else None,
        # 强平今天不发 SSE（与现状一致）
    )


# ─────────────────────────────────────────────────────────────────────────────
# WP5 统一强平：一个 market 的整组持仓作为一个卖出批次
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LiquidateGroupCmd:
    """统一强平命令（计划 §WP5 冻结签名）。

    - ``mode``：``"partial"`` 按 ``partial_pct`` 分批（F12 向上取整到 1 股、封顶持仓）；
      ``"full"`` 在 ``E <= 0`` 时全卖选中组。
    - ``fee_rate``：该产品**普通卖出费率**（F5；LMSR 取 ``site_config.sell_fee_rate``），
      由编排方读取后传入——op 内不读配置，保证报价与执行同源。
    - ``(run_id, round_no)``：业务幂等键，在用户行锁内、卖出**之前**检查。
    - ``state`` 侧（MarketWriter）：本 op 只被该 market 的 consumer 串行调用，
      不额外持锁、不等待其它 writer 队列。
    """

    market_id: int
    user_id: int
    run_id: int
    round_no: int
    mode: str                       # "partial" | "full"
    partial_pct: Decimal
    fee_rate: Decimal
    daily_rate: Decimal = Decimal("0")
    trigger_source: str = "scheduler"
    revalidate_account: bool = False


def _cmd_decimal(value: object, name: str) -> Decimal:
    """把命令里的费率/比例解析成有限 Decimal；非法 → 422（事务外失败）。"""
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=f"{name} 非法: {value!r}") from exc
    if not parsed.is_finite():
        raise HTTPException(status_code=422, detail=f"{name} 必须是有限数字")
    return parsed


async def _find_group_action(
    session, *, run_id: int, round_no: int, user_id: int,
) -> Optional[LiquidationAction]:
    """业务幂等预检：``(run_id, round_no)`` 已存在则返回既有动作。

    必须在**持有用户行锁**之后、卖出之前调用：同一用户的动作被 user 行锁串行化，
    读到的既有动作一定是已提交的（不会看到别的执行者未提交的卖出）。
    额外按 ``user_id`` 过滤：即使调用方传了别人的 run_id，也不可能回放他人的动作。
    """
    return (await session.execute(
        select(LiquidationAction).where(
            LiquidationAction.run_id == int(run_id),
            LiquidationAction.round_no == int(round_no),
            LiquidationAction.user_id == int(user_id),
        )
    )).scalars().first()


async def _lock_group_run(session, cmd: LiquidateGroupCmd) -> Optional[LiquidationRun]:
    """锁 run 行并校验归属；返回 None 表示该 id 尚不存在（调用方已持 user 锁）。

    调用约束来自 ``credit/runs.py``：``get_or_create_active_run`` 会自己锁 User 行，
    必须由已经持锁的调用方在同一事务内调用，否则与执行路径锁序相反。
    """
    run = (await session.execute(
        select(LiquidationRun)
        .where(LiquidationRun.id == int(cmd.run_id))
        .with_for_update()
    )).scalars().first()
    if run is not None and int(run.user_id) != int(cmd.user_id):
        raise HTTPException(status_code=409, detail="强平 run 不属于该用户")
    return run


async def _create_group_run(session, cmd: LiquidateGroupCmd) -> LiquidationRun:
    """run 缺失时补建（调用方已持 user 锁，锁序 User → run）；id 不一致即拒绝。"""
    created = await get_or_create_active_run(
        session, user_id=int(cmd.user_id), trigger_source=str(cmd.trigger_source),
        now=datetime.now(timezone.utc),
    )
    if int(created.id) != int(cmd.run_id):
        raise HTTPException(
            status_code=409, detail="强平 run 缺失且与当前活动 run 不一致，需重新编排")
    return created


def _group_response(
    *, mode: str, sold_count: int, gross: Decimal, fee: Decimal, net: Decimal,
    repaid: Decimal, debt_after: Decimal, cash_after: Decimal,
    blocked_reason: Optional[str], replayed: bool,
) -> OpOutcome:
    """统一响应体（计划 §WP5：sold_count/gross/fee/net/repaid/debt_after/cash_after）。"""
    return OpOutcome(response={
        "sold_count": int(sold_count),
        "gross": gross,
        "fee": fee,
        "net": net,
        "repaid": repaid,
        "debt_after": debt_after,
        "cash_after": cash_after,
        "blocked_reason": blocked_reason,
        "mode": mode,
        "replayed": replayed,
    })


def _replay_group_outcome(action: LiquidationAction, cmd: LiquidateGroupCmd) -> OpOutcome:
    """幂等命中：原样回放已提交动作，不对 DB / 内存镜像做任何变更。

    回放**不**返回 ``new_q_dec``：DB 与 writer 内存镜像的一致性由 writer 自身
    的 commit→apply / reload_state 自愈保证；重放旧 q 反而可能覆盖之后的成交。
    """
    executed = action.executed or {}
    gross = Decimal(str(action.proceeds or ZERO))
    fee = Decimal(str(action.fee or ZERO))
    return _group_response(
        mode=str(action.mode or cmd.mode),
        sold_count=int(executed.get("sold_count") or 0),
        gross=gross, fee=fee, net=gross - fee,
        repaid=Decimal(str(action.repaid or ZERO)),
        debt_after=Decimal(str(action.debt_after or ZERO)),
        cash_after=Decimal(str(action.cash_after or ZERO)),
        blocked_reason=action.blocked_reason,
        replayed=True,
    )


async def op_liquidate_group(state: MarketState, cmd: LiquidateGroupCmd) -> OpOutcome:
    """统一 LMSR 组强平（计划 §WP5；spec §5 F3/F5、§6.2 组合清算）。

    事务内顺序（全部成功才 commit；任何异常整批回滚，内存镜像不动）：

    1. ``lock_user``（User 行锁）→ **幂等预检** ``(run_id, round_no)``；
    2. 该 market 全部持仓 ``FOR UPDATE``（``Position.id ASC``）；
    3. ``quote_lmsr_group`` 在 ``state.q_dec``（writer 权威内存值）的滚动副本上
       按 ``outcome_id`` 升序报价：F12 向上取整到 1 股并封顶持仓；负收益腿跳过
       （不删持仓、不进 q 副本）；市场不可交易 → 整组阻塞且零写入；
    4. 逐腿：``cash += net``（F5：fee 只从 gross 扣一次）、outcome 绝对值 SET、
       ``LIQUIDATE`` 交易（``cost = -net``、``gross`` 为腿 gross、``fee`` 为腿费）、
       同事务审计 ``trade_liquidate`` + 市场快照（逐腿滚动 q）；
    5. 计息 + ``decrease_debt_locked`` 还债（同事务），审计 ``liquidation_repay``；
    6. ``bump_economic_version`` → ``record_action``（``sell_group``）→ 记录型审计
       ``liquidation_action``。**资金事件必须排在记录型事件之前**（audit_replay 锚点）。

    不检查滑点 / 不用用户 ``min_out``、不写 candle；commit 后由 consumer 依据
    ``new_q_dec`` 调 ``feed_prices``（强平改价但无成交事件）。
    """
    mode = str(cmd.mode)
    if mode not in ("partial", "full"):
        raise HTTPException(status_code=422, detail=f"未知强平模式: {cmd.mode!r}")
    if int(cmd.run_id) <= 0:
        raise HTTPException(status_code=422, detail="run_id 必须为正整数")
    if int(cmd.round_no) < 1:
        raise HTTPException(status_code=422, detail="round_no 必须 >= 1")
    trigger_source = str(cmd.trigger_source)
    if not trigger_source or len(trigger_source) > 32:
        raise HTTPException(status_code=422, detail="trigger_source 必须为 1-32 字符")

    fee_rate = _cmd_decimal(cmd.fee_rate, "fee_rate")
    if not (ZERO <= fee_rate < ONE):
        raise HTTPException(status_code=422, detail="fee_rate 必须在 [0, 1)")
    partial_pct = _cmd_decimal(cmd.partial_pct, "partial_pct")
    if mode == "partial" and not (ZERO < partial_pct <= ONE):
        raise HTTPException(status_code=422, detail="partial_pct 必须在 (0, 1]")

    market_id = int(state.market_id)
    index_of = {int(oid): i for i, oid in enumerate(state.outcome_ids)}

    async with async_session_maker() as session:
        async with session.begin():
            locked_user = await lock_user(session, cmd.user_id)
            OWNERSHIP.require_writes()
            version_before = locked_user.economic_version

            # ── 1. 幂等预检：卖之前查业务键（用户行锁内；不是卖完再查）──
            # 先锁 run 行并校验归属，避免用别人的 run_id 回放别人的动作。
            run = await _lock_group_run(session, cmd)
            replay = await _find_group_action(
                session, run_id=int(cmd.run_id), round_no=int(cmd.round_no),
                user_id=int(cmd.user_id))
            if replay is not None:
                logger.info(
                    "LIQUIDATE_GROUP replay user_id=%s market_id=%s run_id=%s round_no=%s "
                    "action_id=%s kind=%s",
                    cmd.user_id, market_id, cmd.run_id, cmd.round_no, replay.id, replay.kind,
                )
                return _replay_group_outcome(replay, cmd)
            if run is not None and run.status != "active":
                # 已提交动作的轮次在上面 replay 掉了；这里是"终态 run + 新轮次"
                raise HTTPException(
                    status_code=409, detail=f"强平 run 已处于终态 {run.status}，本轮不得再执行")

            account_pre = None
            defer_repay = False
            if cmd.revalidate_account:
                from app.services.credit.execution import prepare_locked
                from app.services.credit.risk import discover_dependencies
                from app.services.credit.keys import GroupKey
                deps = CREDIT_DEPS.get()
                from app.services.credit.gates import GATES
                if (deps is None or deps.economic_version != locked_user.economic_version
                        or not (set(deps.groups) | {GroupKey("lmsr", market_id)})
                        <= GATES.held_keys_by_current_task()):
                    raise CreditRetry()
                fresh = await discover_dependencies(session, cmd.user_id)
                if set(fresh.groups) != set(deps.groups):
                    raise CreditRetry()
                plan = await prepare_locked(
                    session, locked_user, fresh, rate=cmd.daily_rate, pct=partial_pct,
                    source=trigger_source, run_id=cmd.run_id, round_no=cmd.round_no)
                run, account_pre = plan.run, plan.pre
                if plan.status != "sell":
                    if plan.status == "cover":
                        # The ranked cover ranks above this market; release this
                        # writer round without touching the market so the caller
                        # rediscovers and executes the cover directly.
                        return _group_response(mode=plan.mode or mode, sold_count=0,
                            gross=ZERO, fee=ZERO, net=ZERO, repaid=ZERO,
                            debt_after=locked_user.debt, cash_after=locked_user.cash,
                            blocked_reason="selection_changed", replayed=False)
                    # Report the *actual* amount repaid on this path, never the
                    # phantom ``pre.debt_effective - user.debt`` (interest drift /
                    # partial repayment / selection change made it wrong).
                    return _group_response(mode=plan.mode or mode, sold_count=0,
                        gross=ZERO, fee=ZERO, net=ZERO, repaid=plan.repaid,
                        debt_after=locked_user.debt, cash_after=locked_user.cash,
                        blocked_reason=(plan.blocked_reason if plan.status == "blocked"
                                        else None),
                        replayed=plan.status == "replayed")
                if plan.target != GroupKey("lmsr", market_id):
                    return _group_response(mode=mode, sold_count=0, gross=ZERO, fee=ZERO,
                        net=ZERO, repaid=ZERO, debt_after=locked_user.debt,
                        cash_after=locked_user.cash, blocked_reason="selection_changed", replayed=False)
                mode = plan.mode
                fee_rate = fresh.lmsr_fee_rate
                defer_repay = bool(plan.defer_repay)

            if not market_is_open(state.status, state.closes_at):
                # 与 legacy op 同语义：HALT/SETTLED/已过 closes_at 不强平（用户自己也卖不了）
                logger.warning(
                    "liquidation_skip_non_trading_market(unified) user_id=%s market_id=%s status=%s",
                    cmd.user_id, market_id, state.status,
                )
                return _group_response(
                    mode=mode, sold_count=0, gross=ZERO, fee=ZERO, net=ZERO, repaid=ZERO,
                    debt_after=locked_user.debt, cash_after=locked_user.cash,
                    blocked_reason=BLOCKED_MARKET_NOT_OPEN, replayed=False)

            # ── 2. 该 market 全部持仓行锁（Position.id ASC；与 legacy 同序）──
            positions = (await session.execute(
                select(Position)
                .join(Outcome, Position.outcome_id == Outcome.id)
                .where(Position.user_id == cmd.user_id,
                       Position.amount > ZERO,
                       Outcome.market_id == market_id)
                .order_by(Position.id.asc())
                .with_for_update()
            )).scalars().all()
            if not positions:
                return _group_response(
                    mode=mode, sold_count=0, gross=ZERO, fee=ZERO, net=ZERO, repaid=ZERO,
                    debt_after=locked_user.debt, cash_after=locked_user.cash,
                    blocked_reason=None, replayed=False)

            unknown = [int(p.outcome_id) for p in positions if int(p.outcome_id) not in index_of]
            if unknown:
                # writer 内存 state 与 DB 不一致（数据异常）：整组阻塞、零写入
                logger.error(
                    "liquidation_unknown_outcome(unified) user_id=%s market_id=%s outcomes=%s",
                    cmd.user_id, market_id, unknown,
                )
                return _group_response(
                    mode=mode, sold_count=0, gross=ZERO, fee=ZERO, net=ZERO, repaid=ZERO,
                    debt_after=locked_user.debt, cash_after=locked_user.cash,
                    blocked_reason=BLOCKED_UNKNOWN_OUTCOME, replayed=False)

            # ── 3. 组级滚动 q 报价（纯函数；q 基准 = writer 权威 state.q_dec）──
            snapshots = [
                OutcomeSnapshot(
                    outcome_id=int(oid),
                    total_shares=quantize_cost(state.q_dec[i]),
                    status=str(getattr(state.status, "value", state.status)),
                    closes_at=state.closes_at,
                )
                for i, oid in enumerate(state.outcome_ids)
            ]
            held = {int(p.outcome_id): p.amount for p in positions}
            quote = quote_lmsr_group(
                snapshots, held,
                market_id=market_id, b=state.b, fee_rate=fee_rate,
                mode=mode, partial_pct=partial_pct, unit=ONE,
            )
            if quote.blocked_reason is not None:
                logger.warning(
                    "liquidation_group_blocked user_id=%s market_id=%s reason=%s",
                    cmd.user_id, market_id, quote.blocked_reason,
                )
                return _group_response(
                    mode=mode, sold_count=0, gross=ZERO, fee=ZERO, net=ZERO, repaid=ZERO,
                    debt_after=locked_user.debt, cash_after=locked_user.cash,
                    blocked_reason=quote.blocked_reason, replayed=False)
            if not quote.legs:
                # 没有正回收腿（held 非空时只可能是全部持仓 amount<=0）
                return _group_response(
                    mode=mode, sold_count=0, gross=ZERO, fee=ZERO, net=ZERO, repaid=ZERO,
                    debt_after=locked_user.debt, cash_after=locked_user.cash,
                    blocked_reason=None, replayed=False)

            # ── 4. run 缺失才补建（正常编排方已先建好；有动作的轮次已 replay）──
            if run is None:
                run = await _create_group_run(session, cmd)

            pos_by_outcome = {int(p.outcome_id): p for p in positions}
            q_dec_roll = [quantize_cost(x) for x in state.q_dec]
            q_float_roll = [float(x) for x in state.q_dec]
            prices = list(state.prices)

            for leg in quote.legs:
                idx = index_of[int(leg.outcome_id)]
                pos = pos_by_outcome[int(leg.outcome_id)]

                # 持仓：全卖删除、部分卖按真实比例减 cost_basis（与 legacy 逐字同口径）
                pos_deleted = leg.amount >= pos.amount
                if pos_deleted:
                    await session.delete(pos)
                else:
                    cost_reduced = (pos.cost_basis * leg.amount / pos.amount).quantize(Q6)
                    pos.amount = pos.amount - leg.amount
                    pos.cost_basis = pos.cost_basis - cost_reduced

                # 现金：F5 净额入账（fee 已在报价里从 gross 扣过一次，不再重复扣）
                locked_user.cash = locked_user.cash + leg.net
                q_dec_roll[idx] = quantize_cost(q_dec_roll[idx] - leg.amount)
                q_float_roll[idx] -= float(leg.amount)
                _, prices_after = calculate_lmsr_with_prices(q_float_roll, state.b)

                avg_price = (
                    quantize_price(leg.gross / leg.amount) if leg.amount > ZERO else ZERO)
                liq_tx = Transaction(
                    user_id=cmd.user_id, outcome_id=int(leg.outcome_id),
                    type=TransactionType.LIQUIDATE, shares=leg.amount,
                    cost=-leg.net, price=avg_price,
                    pre_market_price=quantize_price(prices[idx]),
                    post_market_price=quantize_price(prices_after[idx]),
                    gross=leg.gross, fee=leg.fee,
                    market_prices_post=list(prices_after),
                )
                session.add(liq_tx)
                await audit_service.record_trade(
                    session, tx=liq_tx, user=locked_user,
                    position=None if pos_deleted else pos,
                    market_id=market_id,
                    market_after=audit_service.market_snapshot(
                        outcome_ids=state.outcome_ids, q=q_dec_roll, b=state.b,
                        prices=prices_after, status=state.status),
                    extra={"mode": mode, "partial_pct": partial_pct, "fee_rate": fee_rate,
                           "run_id": int(cmd.run_id), "round_no": int(cmd.round_no),
                           "path": "unified"},
                )
                prices = prices_after

            # 镜像批量 SET（每个动过的 outcome 一条 UPDATE；值取 6dp 滚动 q）
            for i, oid in enumerate(state.outcome_ids):
                if q_dec_roll[i] != state.q_dec[i]:
                    await session.execute(
                        sa_update(Outcome).where(Outcome.id == int(oid))
                        .values(total_shares=q_dec_roll[i]))

            # ── 5. 回款还债（同事务；先结息再算还款额，不留灰尘债）──
            # 口径：有待回补空头时把净回款留作下一轮回补预算（spec §8.1 第 1/4 步），
            # 只结息不还金债；否则沿用旧的立即还债 + 重估。有空头时只能用未锁现金。
            repaid = ZERO
            has_foreign = account_pre is not None and any(
                g.role == "short_cover" for g in account_pre.groups)
            if defer_repay:
                from app.services.credit.execution import accrue_gold_only
                await accrue_gold_only(session, locked_user, cmd.daily_rate, trigger_source)
                repay_cap = None
            else:
                # Cap at post-sale free cash, never the pre-sale snapshot: the sale
                # just credited net proceeds while every short lock is unchanged,
                # so ``account_pre.available_cash`` would under-pay gold debt.
                if has_foreign:
                    from app.services.credit.execution import post_sale_free_cash
                    repay_cap = post_sale_free_cash(locked_user, account_pre)
                else:
                    repay_cap = None
            if not defer_repay and locked_user.cash > ZERO and locked_user.debt > ZERO:
                from app.services import loan_service   # 局部 import 避免环
                debt_before = locked_user.debt
                now = loan_service._compat_now(locked_user)
                loan_service.accrue_interest(locked_user, cmd.daily_rate, now)
                repay_amount = min(locked_user.cash, locked_user.debt)
                if repay_cap is not None:
                    repay_amount = min(repay_amount, repay_cap)
                repay_amount = repay_amount.quantize(Q6)
                if repay_amount > ZERO:
                    repaid = await loan_service.decrease_debt_locked(
                        session, locked_user, repay_amount,
                        consume_cash=True, daily_rate=cmd.daily_rate, now=now)
                    audit_service.record_liquidation_repay(
                        session, locked_user, repaid, debt_before,
                        cmd.daily_rate, trigger_source)

            # ── 6. 版本 + 动作记录 + 记录型审计（必须晚于资金事件）──
            version = (bump_economic_version(locked_user)
                       if locked_user.economic_version == version_before
                       else locked_user.economic_version)
            executed = {
                "sold_count": len(quote.legs),
                "gross": format(quote.gross, "f"),
                "fee": format(quote.fee, "f"),
                "net": format(quote.net, "f"),
                "legs": [
                    {
                        "outcome_id": int(leg.outcome_id),
                        "shares": format(leg.amount, "f"),
                        "gross": format(leg.gross, "f"),
                        "fee": format(leg.fee, "f"),
                        "net": format(leg.net, "f"),
                    }
                    for leg in quote.legs
                ],
                "q_after": [format(x, "f") for x in q_dec_roll],
            }
            action = await record_action(
                session, run=run, round_no=int(cmd.round_no), kind="sell_group",
                product="lmsr", group_id=market_id, mode=mode,
                requested={"mode": mode, "partial_pct": format(partial_pct, "f"),
                           "trigger_source": trigger_source},
                executed=executed,
                proceeds=quote.gross, fee=quote.fee, fee_currency="gold",
                repaid=repaid, debt_after=locked_user.debt, cash_after=locked_user.cash,
                economic_version_after=version,
            )
            if action.executed != executed:
                # 用户行锁下理论不可达；DB 唯一键兜底防"卖出后才发现本轮已被占用"
                raise HTTPException(status_code=409, detail="强平轮次已被占用，本次已回滚")

            if account_pre is not None:
                from app.services.credit.execution import public_event, finish_locked
                public_event(session, locked_user, run, account_pre, product="lmsr",
                    mode=mode, sold=len(quote.legs), proceeds=quote.net,
                    repaid=repaid, source=trigger_source)
                await finish_locked(session, locked_user, run, cmd.daily_rate)
                action.economic_version_after = locked_user.economic_version

            audit_service.record(
                session, "liquidation_action",
                user_id=int(locked_user.id),
                ref_table="liquidation_action", ref_id=int(action.id),
                payload={
                    "run_id": int(run.id), "round_no": int(cmd.round_no),
                    "kind": "sell_group", "product": "lmsr", "group_id": market_id,
                    "mode": mode, "sold_count": len(quote.legs),
                    "proceeds": format(quote.gross, "f"), "fee": format(quote.fee, "f"),
                    "fee_currency": "gold", "net": format(quote.net, "f"),
                    "repaid": format(repaid, "f"),
                    "debt_after": format(locked_user.debt, "f"),
                    "cash_after": format(locked_user.cash, "f"),
                    "economic_version_after": int(action.economic_version_after),
                    "trigger_source": trigger_source,
                },
                user_after=audit_service.user_snapshot(locked_user),
            )

        new_cash = locked_user.cash
        new_debt = locked_user.debt

    logger.info(
        "LIQUIDATE_GROUP(writer) user_id=%s market_id=%s run_id=%s round_no=%s mode=%s "
        "sold_count=%s gross=%s fee=%s net=%s repaid=%s cash_after=%s debt_after=%s",
        cmd.user_id, market_id, cmd.run_id, cmd.round_no, mode, len(quote.legs),
        quote.gross, quote.fee, quote.net, repaid, new_cash, new_debt,
    )
    return OpOutcome(
        response={
            "sold_count": len(quote.legs),
            "gross": quote.gross,
            "fee": quote.fee,
            "net": quote.net,
            "repaid": repaid,
            "debt_after": new_debt,
            "cash_after": new_cash,
            "blocked_reason": None,
            "mode": mode,
            "replayed": False,
        },
        # 无 candle（K 线只记 BUY/SELL）；consumer 见 new_q_dec 且无 tick_trade → feed_prices
        new_q_dec=q_dec_roll,
    )


def register_all_ops(writer: MarketWriter) -> None:
    writer.register_op(BuyCmd, op_buy)
    writer.register_op(SellCmd, op_sell)
    writer.register_op(CloseCmd, op_close)
    writer.register_op(ResumeCmd, op_resume)
    writer.register_op(ResolveCmd, op_resolve)
    writer.register_op(LiquidateMarketCmd, op_liquidate_market)
    writer.register_op(LiquidateGroupCmd, op_liquidate_group)
