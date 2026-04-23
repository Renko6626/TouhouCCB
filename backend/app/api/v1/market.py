import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, and_, func, literal_column
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.services.realtime import BROKER
from app.core.database import get_async_session, managed_transaction
from app.core.users import current_active_user, current_superuser
from app.models.base import User, Market, Outcome, Position, Transaction, MarketStatus, TransactionType
from app.schemas.market import (
    MarketCreate,
    TradeRequest,
    TradeResponse,
    ResolveRequest,
    SettleResult,
    MarketDetailRead,
    OutcomeQuoteRead,
    QuoteRequest,
    QuoteResponse,
    LeaderboardItem,
    MarketTradeRead,
    RecentTradeRead,
    MoverItem,
)
from app.services.lmsr import calculate_lmsr_cost, get_current_price, quantize_cost, quantize_price

logger = logging.getLogger(__name__)

router = APIRouter()

SELL_FEE_RATE = Decimal("0")
ZERO = Decimal("0")


# -----------------------------
# Helpers
# -----------------------------

async def _lock_market(db: AsyncSession, market_id: int) -> Market:
    """锁住市场行，保证 status 等状态机不会被并发改乱。"""
    res = await db.execute(
        select(Market).where(Market.id == market_id).with_for_update()
    )
    market = res.scalars().first()
    if not market:
        raise HTTPException(status_code=404, detail="市场不存在")
    return market


async def _lock_user(db: AsyncSession, user_id: int) -> User:
    """锁住用户行，保证 cash 扣减不会被并发穿透。"""
    res = await db.execute(
        select(User).where(User.id == user_id).with_for_update()
    )
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


async def _lock_outcomes_for_market(db: AsyncSession, market_id: int) -> List[Outcome]:
    """锁住该市场所有 outcome 行（total_shares 是 LMSR 状态）。"""
    res = await db.execute(
        select(Outcome)
        .where(Outcome.market_id == market_id)
        .order_by(Outcome.id)
        .with_for_update()
    )
    outcomes = res.scalars().all()
    if not outcomes:
        raise HTTPException(status_code=404, detail="市场选项不存在（数据异常）")
    return outcomes


async def _lock_outcome(db: AsyncSession, outcome_id: int) -> Outcome:
    """锁住单个 outcome 行，返回对应记录。"""
    res = await db.execute(
        select(Outcome).where(Outcome.id == outcome_id).with_for_update()
    )
    outcome = res.scalars().first()
    if not outcome:
        raise HTTPException(status_code=404, detail="选项不存在")
    return outcome


async def _get_prices_24h_ago(db: AsyncSession, outcome_ids: List[int]) -> Dict[int, float]:
    """
    批量获取每个 outcome 在 24h 前的最后成交价。
    返回 {outcome_id: price}，无成交的 outcome 不在 dict 中。
    """
    if not outcome_ids:
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    # 用窗口函数取每个 outcome 在 cutoff 之前的最后一笔成交价
    # row_number() OVER (PARTITION BY outcome_id ORDER BY timestamp DESC) = 1
    sub = (
        select(
            Transaction.outcome_id,
            Transaction.price,
            func.row_number().over(
                partition_by=Transaction.outcome_id,
                order_by=Transaction.timestamp.desc(),
            ).label("rn"),
        )
        .where(
            and_(
                Transaction.outcome_id.in_(outcome_ids),
                Transaction.timestamp <= cutoff,
            )
        )
        .subquery()
    )

    stmt = select(sub.c.outcome_id, sub.c.price).where(sub.c.rn == 1)
    res = await db.execute(stmt)
    return {row[0]: float(row[1]) for row in res.all() if row[1] and float(row[1]) > 0}


def _shares_to_floats(outcomes: List[Outcome]) -> List[float]:
    """Outcome.total_shares (Decimal) → float list，喂给 LMSR。"""
    return [float(o.total_shares) for o in outcomes]


def _build_prices_from_shares(
    outcomes: List[Outcome],
    shares_list: List[float],
    b: float,
    prices_24h_ago: Optional[Dict[int, float]] = None,
) -> List[Dict[str, Any]]:
    out = []
    for i, o in enumerate(outcomes):
        p = get_current_price(shares_list, i, b)
        cur = float(quantize_price(p))
        entry: Dict[str, Any] = {
            "id": o.id,
            "label": o.label,
            "shares": float(o.total_shares),
            "current_price": cur,
        }
        if prices_24h_ago is not None:
            prev = prices_24h_ago.get(o.id)
            if prev is not None and prev > 0:
                entry["price_change_24h"] = round(cur - prev, 8)
                entry["price_change_pct_24h"] = round((cur - prev) / prev * 100, 2)
            else:
                entry["price_change_24h"] = None
                entry["price_change_pct_24h"] = None
        out.append(entry)
    return out


def _require_trading(market: Market):
    if market.status != MarketStatus.TRADING:
        raise HTTPException(status_code=400, detail="市场当前不可交易")
    if market.closes_at and datetime.now(timezone.utc) >= market.closes_at:
        raise HTTPException(status_code=400, detail="市场已过交易截止时间")


# ==========================================
# 管理员接口
# ==========================================

@router.post("/create", status_code=status.HTTP_201_CREATED, summary="创建新市场（仅限管理员）")
async def create_market(
    data: MarketCreate,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    new_market = Market(
        title=data.title,
        description=data.description,
        liquidity_b=data.liquidity_b,
        status=MarketStatus.TRADING,
        closes_at=data.closes_at,
        tags=",".join(data.tags) if data.tags else "",
    )
    db.add(new_market)
    await db.flush()

    for label in data.outcomes:
        db.add(Outcome(market_id=new_market.id, label=label, total_shares=ZERO))

    await db.commit()
    return {
        "status": "success",
        "market_id": new_market.id,
        "title": new_market.title,
        "outcomes": data.outcomes,
        "created_by": admin.username,
    }


@router.post("/{market_id:int}/close", summary="关闭市场交易（仅限管理员）")
async def close_market(
    market_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    async with managed_transaction(db):
        market = await _lock_market(db, market_id)
        if market.status == MarketStatus.SETTLED:
            raise HTTPException(status_code=400, detail="市场已结算，无法熔断")
        market.status = MarketStatus.HALT
    await BROKER.publish(
        market_id,
        "market_status",
        {"status": MarketStatus.HALT}
    )
    return {"message": f"市场 {market.title} 已停止交易（熔断）"}


# ==========================================
# 公共接口
# ==========================================

@router.get("/list", summary="获取所有活跃市场")
async def list_markets(
    keyword: str = Query(None, description="按标题搜索"),
    tag: str = Query(None, description="按标签过滤"),
    include_halt: bool = Query(False, description="是否包含熔断中的市场"),
    include_settled: bool = Query(False, description="是否包含已结算的市场"),
    db: AsyncSession = Depends(get_async_session),
):
    allowed = [MarketStatus.TRADING]
    if include_halt:
        allowed.append(MarketStatus.HALT)
    if include_settled:
        allowed.append(MarketStatus.SETTLED)

    stmt = (
        select(Market)
        .where(Market.status.in_(allowed))
        .options(selectinload(Market.outcomes))
        .order_by(Market.created_at.desc())
    )

    if keyword:
        stmt = stmt.where(Market.title.contains(keyword))
    if tag:
        stmt = stmt.where(Market.tags.contains(tag))

    res = await db.execute(stmt)
    markets = res.scalars().all()

    # 批量查 24h 前价格
    all_outcome_ids = [o.id for m in markets for o in m.outcomes]
    prices_24h = await _get_prices_24h_ago(db, all_outcome_ids)

    # 批量聚合：每个 market 的成交笔数 + 最后成交时间。
    # 只统计真实用户交易（buy/sell），结算产生的 settle/settle_lose 不算活跃度。
    market_ids = [m.id for m in markets]
    activity: Dict[int, Dict[str, Any]] = {}
    if market_ids:
        agg_stmt = (
            select(
                Outcome.market_id.label("market_id"),
                func.count(Transaction.id).label("trade_count"),
                func.max(Transaction.timestamp).label("last_trade_at"),
            )
            .select_from(Outcome)
            .outerjoin(
                Transaction,
                and_(
                    Transaction.outcome_id == Outcome.id,
                    Transaction.type.in_([TransactionType.BUY, TransactionType.SELL]),
                ),
            )
            .where(Outcome.market_id.in_(market_ids))
            .group_by(Outcome.market_id)
        )
        for row in (await db.execute(agg_stmt)).all():
            activity[row.market_id] = {
                "trade_count": int(row.trade_count or 0),
                "last_trade_at": row.last_trade_at,
            }

    output = []
    for m in markets:
        outcomes = list(m.outcomes)
        shares_list = _shares_to_floats(outcomes)
        tags_list = [t.strip() for t in m.tags.split(",") if t.strip()] if m.tags else []
        act = activity.get(m.id, {})
        last_ts = act.get("last_trade_at")
        output.append({
            "id": m.id,
            "title": m.title,
            "description": m.description,
            "liquidity_b": float(m.liquidity_b),
            "status": m.status,
            "closes_at": m.closes_at.isoformat() if m.closes_at else None,
            "tags": tags_list,
            "outcomes": _build_prices_from_shares(outcomes, shares_list, float(m.liquidity_b), prices_24h),
            "trade_count": act.get("trade_count", 0),
            "last_trade_at": last_ts.isoformat() if last_ts else None,
        })
    return output


@router.get("/{market_id:int}", response_model=MarketDetailRead, summary="获取市场详情")
async def get_market_detail(
    market_id: int,
    db: AsyncSession = Depends(get_async_session),
):
    market = await db.get(Market, market_id)
    if not market:
        raise HTTPException(status_code=404, detail="市场不存在")

    o_res = await db.execute(
        select(Outcome)
        .where(Outcome.market_id == market.id)
        .order_by(Outcome.id.asc())
    )
    outcomes = o_res.scalars().all()
    if not outcomes:
        raise HTTPException(status_code=400, detail="市场选项异常：无 outcomes")

    shares_list = _shares_to_floats(outcomes)
    b = float(market.liquidity_b)

    # 24h 前价格
    outcome_ids = [o.id for o in outcomes]
    prices_24h = await _get_prices_24h_ago(db, outcome_ids)

    out_reads: list[OutcomeQuoteRead] = []
    for i, o in enumerate(outcomes):
        price = quantize_price(get_current_price(shares_list, i, b))
        is_winner = None
        if getattr(market, "winning_outcome_id", None) is not None:
            is_winner = (int(market.winning_outcome_id) == int(o.id))

        prev = prices_24h.get(o.id)
        cur_f = float(price)
        if prev is not None and prev > 0:
            change = round(cur_f - prev, 8)
            change_pct = round((cur_f - prev) / prev * 100, 2)
        else:
            change = None
            change_pct = None

        out_reads.append(
            OutcomeQuoteRead(
                id=int(o.id),
                label=str(o.label),
                total_shares=o.total_shares,
                current_price=price,
                payout=o.payout,
                is_winner=is_winner,
                price_change_24h=change,
                price_change_pct_24h=change_pct,
            )
        )

    # last_trade_at
    if hasattr(Transaction, "market_id"):
        tx_stmt = (
            select(Transaction.timestamp)
            .where(Transaction.market_id == market.id)
            .order_by(Transaction.timestamp.desc())
            .limit(1)
        )
    else:
        tx_stmt = (
            select(Transaction.timestamp)
            .join(Outcome, Transaction.outcome_id == Outcome.id)
            .where(Outcome.market_id == market.id)
            .order_by(Transaction.timestamp.desc())
            .limit(1)
        )
    tx_res = await db.execute(tx_stmt)
    last_trade_at = tx_res.scalar_one_or_none()

    return MarketDetailRead(
        id=int(market.id),
        title=str(market.title),
        description=str(market.description or ""),
        status=str(market.status),
        liquidity_b=float(market.liquidity_b),
        created_at=market.created_at.replace(tzinfo=timezone.utc) if market.created_at.tzinfo is None else market.created_at,
        closes_at=market.closes_at,
        tags=[t.strip() for t in market.tags.split(",") if t.strip()] if market.tags else [],

        winning_outcome_id=getattr(market, "winning_outcome_id", None),
        settled_at=getattr(market, "settled_at", None),
        settled_by_user_id=getattr(market, "settled_by_user_id", None),

        outcomes=out_reads,
        last_trade_at=last_trade_at,
    )


# ==========================================
# 交易接口（登录用户）
# ==========================================

@router.post("/buy", response_model=TradeResponse, summary="买入胜券")
async def buy_shares(
    req: TradeRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    shares_d = quantize_cost(req.shares)
    if shares_d <= ZERO:
        raise HTTPException(status_code=422, detail="shares 必须为正数")

    async with managed_transaction(db):
        outcome = await _lock_outcome(db, int(req.outcome_id))
        market = await _lock_market(db, int(outcome.market_id))
        _require_trading(market)

        all_outcomes = await _lock_outcomes_for_market(db, int(market.id))
        target_idx = next((i for i, o in enumerate(all_outcomes) if o.id == outcome.id), None)
        if target_idx is None:
            raise HTTPException(status_code=400, detail="选项不属于该市场（数据异常）")

        locked_user = await _lock_user(db, int(user.id))

        # LMSR 用 float 计算
        b = float(market.liquidity_b)
        old_q = _shares_to_floats(all_outcomes)

        new_q = list(old_q)
        new_q[target_idx] += float(shares_d)

        old_cost_f = calculate_lmsr_cost(old_q, b)
        new_cost_f = calculate_lmsr_cost(new_q, b)

        # 边界：float → Decimal
        pay = quantize_cost(new_cost_f - old_cost_f)
        if pay <= ZERO:
            raise HTTPException(status_code=400, detail="订单异常：成本不应为非正")

        if locked_user.cash < pay:
            raise HTTPException(status_code=400, detail="现金不足")

        # Decimal 精确运算
        locked_user.cash -= pay
        all_outcomes[target_idx].total_shares += shares_d

        # 持仓
        pos_res = await db.execute(
            select(Position)
            .where(Position.user_id == locked_user.id, Position.outcome_id == outcome.id)
            .with_for_update()
        )
        position = pos_res.scalars().first()
        if not position:
            position = Position(user_id=locked_user.id, outcome_id=outcome.id, amount=ZERO, cost_basis=ZERO)
            db.add(position)
        position.amount += shares_d
        position.cost_basis += pay

        # 交易记录（Decimal 直除，避免 float 往返）
        avg_price = quantize_price(pay / shares_d)
        # 交易前后瞬时市场价（K线用：open=pre, close=post）
        pre_mp = quantize_price(get_current_price(old_q, target_idx, b))
        post_mp = quantize_price(get_current_price(new_q, target_idx, b))

        db.add(Transaction(
            user_id=locked_user.id,
            outcome_id=outcome.id,
            type=TransactionType.BUY,
            shares=shares_d,
            cost=pay,
            price=avg_price,
            pre_market_price=pre_mp,
            post_market_price=post_mp,
            gross=pay,
            fee=ZERO,
        ))

    logger.info(
        "BUY user_id=%s outcome_id=%s market_id=%s shares=%s cost=%s avg_price=%s pre_mp=%s post_mp=%s new_cash=%s",
        user.id, outcome.id, market.id, shares_d, pay, avg_price, pre_mp, post_mp, locked_user.cash,
    )

    await BROKER.publish(
        market.id,
        "trade",
        {
            "trade": {
                "type": TransactionType.BUY,
                "outcome_id": int(outcome.id),
                "shares": float(shares_d),
                "price": float(avg_price),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        }
    )

    return {
        "shares": float(shares_d),
        "cost": float(pay.quantize(Decimal("0.01"))),
        "new_cash": float(locked_user.cash.quantize(Decimal("0.01"))),
        "message": f"成功买入 {shares_d:f} 张 {outcome.label}（均价≈{avg_price}）",
    }


@router.post("/sell", response_model=TradeResponse, summary="卖出胜券")
async def sell_shares(
    req: TradeRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    shares_d = quantize_cost(req.shares)
    if shares_d <= ZERO:
        raise HTTPException(status_code=422, detail="shares 必须为正数")

    async with managed_transaction(db):
        pos_res = await db.execute(
            select(Position)
            .where(Position.user_id == int(user.id), Position.outcome_id == int(req.outcome_id))
            .with_for_update()
        )
        position = pos_res.scalars().first()
        if not position or position.amount < shares_d:
            raise HTTPException(status_code=400, detail="持仓不足")

        outcome = await _lock_outcome(db, int(req.outcome_id))
        market = await _lock_market(db, int(outcome.market_id))
        _require_trading(market)

        all_outcomes = await _lock_outcomes_for_market(db, int(market.id))
        target_idx = next((i for i, o in enumerate(all_outcomes) if o.id == outcome.id), None)
        if target_idx is None:
            raise HTTPException(status_code=400, detail="选项不属于该市场（数据异常）")

        locked_user = await _lock_user(db, int(user.id))

        # LMSR 用 float 计算
        b = float(market.liquidity_b)
        old_q = _shares_to_floats(all_outcomes)
        if old_q[target_idx] < float(shares_d):
            raise HTTPException(status_code=400, detail="市场总份额不足（异常状态）")

        new_q = list(old_q)
        new_q[target_idx] -= float(shares_d)

        old_cost_f = calculate_lmsr_cost(old_q, b)
        new_cost_f = calculate_lmsr_cost(new_q, b)

        # 边界：float → Decimal
        proceeds = quantize_cost(old_cost_f - new_cost_f)
        if proceeds < ZERO:
            proceeds = ZERO

        fee = (proceeds * SELL_FEE_RATE).quantize(Decimal("0.000001"))
        net = proceeds - fee

        # Decimal 精确运算
        locked_user.cash += net
        all_outcomes[target_idx].total_shares -= shares_d

        # cost_basis 按卖出比例减少：卖掉 shares_d / amount 比例的成本
        if position.amount > ZERO:
            sold_ratio = shares_d / position.amount
            position.cost_basis -= (position.cost_basis * sold_ratio).quantize(Decimal("0.000001"))
        position.amount -= shares_d
        # 清仓时确保 cost_basis 归零
        if position.amount <= ZERO:
            position.cost_basis = ZERO

        # 交易记录（Decimal 直除，避免 float 往返）
        avg_price = quantize_price(proceeds / shares_d) if shares_d > ZERO else ZERO
        # 交易前后瞬时市场价（K线用）
        pre_mp = quantize_price(get_current_price(old_q, target_idx, b))
        post_mp = quantize_price(get_current_price(new_q, target_idx, b))

        db.add(Transaction(
            user_id=locked_user.id,
            outcome_id=outcome.id,
            type=TransactionType.SELL,
            shares=shares_d,
            cost=-net,
            price=avg_price,
            pre_market_price=pre_mp,
            post_market_price=post_mp,
            gross=proceeds,
            fee=fee,
        ))

    logger.info(
        "SELL user_id=%s outcome_id=%s market_id=%s shares=%s proceeds=%s fee=%s net=%s avg_price=%s pre_mp=%s post_mp=%s new_cash=%s",
        user.id, outcome.id, market.id, shares_d, proceeds, fee, net, avg_price, pre_mp, post_mp, locked_user.cash,
    )

    await BROKER.publish(
        market.id,
        "trade",
        {
            "trade": {
                "type": TransactionType.SELL,
                "outcome_id": int(outcome.id),
                "shares": float(shares_d),
                "price": float(avg_price),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        }
    )

    return {
        "shares": float(shares_d),
        "cost": float((-net).quantize(Decimal("0.01"))),
        "new_cash": float(locked_user.cash.quantize(Decimal("0.01"))),
        "message": f"卖出成功，获得 {net}（手续费 {fee}，均价≈{avg_price}）",
    }


@router.post(
    "/{market_id:int}/resolve",
    response_model=SettleResult,
    summary="结算市场（指定赢家，发放兑付，仅管理员）",
    status_code=status.HTTP_200_OK,
)
async def resolve_market(
    market_id: int,
    req: ResolveRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    payout_unit = quantize_cost(req.payout)
    if payout_unit < ZERO:
        raise HTTPException(status_code=422, detail="payout 必须 >= 0")

    async with managed_transaction(db):
        m_res = await db.execute(
            select(Market).where(Market.id == market_id).with_for_update()
        )
        market = m_res.scalars().first()
        if not market:
            raise HTTPException(status_code=404, detail="市场不存在")

        if market.status == MarketStatus.SETTLED:
            if market.winning_outcome_id is None or market.settled_at is None:
                raise HTTPException(status_code=500, detail="市场已结算但结算字段缺失（数据异常）")
            return SettleResult(
                market_id=market.id,
                status=market.status,
                winning_outcome_id=int(market.winning_outcome_id),
                settled_at=market.settled_at,
                total_payout=ZERO,
                settled_positions=0,
            )

        o_res = await db.execute(
            select(Outcome)
            .where(Outcome.market_id == market.id)
            .order_by(Outcome.id.asc())
            .with_for_update()
        )
        outcomes = o_res.scalars().all()
        if len(outcomes) < 2:
            raise HTTPException(status_code=400, detail="市场选项数量异常")

        winning = next((o for o in outcomes if o.id == req.winning_outcome_id), None)
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
        p_res = await db.execute(p_stmt)
        positions = p_res.scalars().all()

        # Decimal 计算兑付
        payout_by_user: dict[int, Decimal] = {}
        settled_positions = 0

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
                db.add(Transaction(
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
                ))

            await db.delete(pos)

        total_payout = ZERO
        now = datetime.now(timezone.utc)

        for uid, pay in payout_by_user.items():
            if pay <= ZERO:
                continue

            u_res = await db.execute(
                select(User).where(User.id == uid).with_for_update()
            )
            u = u_res.scalars().first()
            if not u:
                raise HTTPException(status_code=500, detail=f"用户 {uid} 不存在，无法结算（已回滚）")

            u.cash += pay
            total_payout += pay

            db.add(Transaction(
                user_id=u.id,
                outcome_id=winning.id,
                type=TransactionType.SETTLE,
                shares=ZERO,
                gross=pay,
                fee=ZERO,
                price=payout_unit,
                cost=-pay,
                timestamp=now,
            ))

        for o in outcomes:
            o.payout = payout_unit if o.id == winning.id else ZERO

        market.status = MarketStatus.SETTLED
        market.winning_outcome_id = winning.id
        market.settled_at = now
        market.settled_by_user_id = admin.id

    logger.info(
        "RESOLVE market_id=%s winning_outcome_id=%s payout=%s total_payout=%s settled_positions=%s admin_id=%s",
        market.id, winning.id, payout_unit, total_payout, settled_positions, admin.id,
    )

    await BROKER.publish(
        market.id,
        "market_status",
        {
            "status": MarketStatus.SETTLED,
            "winning_outcome_id": int(winning.id),
            "settled_at": now.isoformat(),
        }
    )

    return SettleResult(
        market_id=market.id,
        status=market.status,
        winning_outcome_id=int(market.winning_outcome_id),
        settled_at=market.settled_at,
        total_payout=total_payout,
        settled_positions=int(settled_positions),
    )


@router.post("/quote", response_model=QuoteResponse, summary="下单预估（不真实成交）")
async def quote_trade(
    req: QuoteRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    outcome = await db.get(Outcome, req.outcome_id)
    if not outcome:
        raise HTTPException(status_code=404, detail="选项不存在")

    market = await db.get(Market, outcome.market_id)
    if not market:
        raise HTTPException(status_code=404, detail="市场不存在")

    if market.status != MarketStatus.TRADING:
        raise HTTPException(status_code=400, detail="市场未在交易中，无法报价")

    outcomes_res = await db.execute(
        select(Outcome).where(Outcome.market_id == market.id).order_by(Outcome.id)
    )
    outcomes = outcomes_res.scalars().all()

    idx = next((i for i, o in enumerate(outcomes) if o.id == outcome.id), None)
    if idx is None:
        raise HTTPException(status_code=400, detail="选项不属于该市场（数据异常）")
    b = float(market.liquidity_b)
    shares_d = quantize_cost(req.shares)    # Decimal，用于精确计算
    shares_f = float(req.shares)            # float，喂给 LMSR

    old_q = _shares_to_floats(outcomes)
    old_cost = calculate_lmsr_cost(old_q, b)

    new_q = list(old_q)
    if req.side == "buy":
        new_q[idx] += shares_f
        new_cost = calculate_lmsr_cost(new_q, b)
        gross = quantize_cost(new_cost - old_cost)
        fee = ZERO
        net = gross
    else:
        if new_q[idx] < shares_f:
            raise HTTPException(status_code=400, detail="卖出数量超过市场总份额")
        new_q[idx] -= shares_f
        new_cost = calculate_lmsr_cost(new_q, b)
        gross = quantize_cost(old_cost - new_cost)
        fee = (gross * SELL_FEE_RATE).quantize(Decimal("0.000001"))
        net = gross - fee

    avg_price = quantize_price(gross / shares_d) if shares_d > ZERO else ZERO
    after_prices = _build_prices_from_shares(outcomes, new_q, b)

    return QuoteResponse(
        outcome_id=req.outcome_id,
        side=req.side,
        shares=shares_d,
        avg_price=avg_price,
        gross=gross,
        fee=fee,
        net=net,
        after_prices=after_prices,
    )


@router.get(
    "/{market_id:int}/trades",
    response_model=List[MarketTradeRead],
    summary="市场逐笔成交"
)
async def get_market_trades(
    market_id: int,
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_async_session),
):
    stmt = (
        select(Transaction, User)
        .join(Outcome, Transaction.outcome_id == Outcome.id)
        .join(User, Transaction.user_id == User.id)
        .where(Outcome.market_id == market_id)
        .order_by(Transaction.timestamp.desc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    rows = res.all()

    return [
        MarketTradeRead(
            id=tx.id,
            outcome_id=tx.outcome_id,
            side=tx.type,
            shares=tx.shares,
            price=tx.price,
            gross=tx.gross,
            fee=tx.fee,
            timestamp=tx.timestamp.replace(tzinfo=timezone.utc) if tx.timestamp.tzinfo is None else tx.timestamp,
            username=u.username,
        )
        for tx, u in rows
    ]


@router.post("/{market_id:int}/resume", summary="恢复市场交易（仅管理员）")
async def resume_market(
    market_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    async with managed_transaction(db):
        market = await _lock_market(db, market_id)
        if market.status == MarketStatus.SETTLED:
            raise HTTPException(status_code=400, detail="市场已结算，无法恢复交易")
        if market.status != MarketStatus.HALT:
            raise HTTPException(status_code=400, detail="市场当前不在熔断状态")
        market.status = MarketStatus.TRADING

    await BROKER.publish(
        market.id,
        "market_status",
        {"status": MarketStatus.TRADING}
    )
    return {"message": f"市场 {market.title} 已恢复交易"}


@router.get("/leaderboard", response_model=List[LeaderboardItem], summary="财富排行榜")
async def leaderboard(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_session),
):
    res = await db.execute(
        select(User).order_by((User.cash - User.debt).desc()).limit(limit)
    )
    users = res.scalars().all()

    items = []
    for u in users:
        net = u.cash - u.debt
        rank = "初入幻想乡"
        if net > 500: rank = "人间之里商人"
        if net > 2000: rank = "命莲寺赞助者"
        if net > 10000: rank = "守矢VIP"
        if net > 50000: rank = "大天狗座上宾"

        items.append(LeaderboardItem(
            user_id=u.id,
            username=u.username,
            net_worth=net.quantize(Decimal("0.01")),
            rank=rank
        ))

    return items


# ==========================================
# 首页：实时成交流 + 涨跌榜
# ==========================================

@router.get("/recent-trades", response_model=List[RecentTradeRead], summary="跨市场最近成交")
async def recent_trades(
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_async_session),
):
    """返回最近 N 笔买/卖成交（不含结算系统单），跨所有市场，倒序按时间。"""
    stmt = (
        select(Transaction, Outcome, Market, User)
        .join(Outcome, Transaction.outcome_id == Outcome.id)
        .join(Market, Outcome.market_id == Market.id)
        .join(User, Transaction.user_id == User.id)
        .where(Transaction.type.in_([TransactionType.BUY, TransactionType.SELL]))
        .order_by(Transaction.timestamp.desc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    rows = res.all()
    return [
        RecentTradeRead(
            id=tx.id,
            timestamp=tx.timestamp.replace(tzinfo=timezone.utc) if tx.timestamp.tzinfo is None else tx.timestamp,
            market_id=mk.id,
            market_title=mk.title,
            outcome_id=oc.id,
            outcome_label=oc.label,
            type=tx.type,
            shares=tx.shares,
            price=tx.price,
            username=u.username,
        )
        for tx, oc, mk, u in rows
    ]


_MOVER_WINDOW_SECONDS: Dict[str, int] = {
    "10min": 600,
    "1h": 3600,
    "24h": 86400,
}


@router.get("/movers", response_model=List[MoverItem], summary="涨跌榜（按时间窗口）")
async def movers(
    window: str = Query("24h", pattern="^(10min|1h|24h)$", description="时间窗口"),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_async_session),
):
    """
    返回所有 trading 市场所有 outcome 的价格变动榜，按 |change_pct| 倒序。
    基准价：每个 outcome 在 cutoff 之前最后一笔交易的 post_market_price；
    若该 outcome 无 cutoff 之前的交易记录，则回退到初始等概率 1/N（仅当
    整个市场也无 cutoff 之前交易时严格正确，否则为近似）。
    """
    seconds = _MOVER_WINDOW_SECONDS[window]
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)

    # 1) 取所有 trading 市场
    m_res = await db.execute(
        select(Market)
        .where(Market.status == MarketStatus.TRADING)
        .options(selectinload(Market.outcomes))
    )
    markets = m_res.scalars().all()
    if not markets:
        return []

    # 2) 批量查每个 outcome 在 cutoff 前的最后一笔成交价（窗口函数）
    all_outcome_ids = [o.id for m in markets for o in m.outcomes]
    if not all_outcome_ids:
        return []

    sub = (
        select(
            Transaction.outcome_id,
            Transaction.post_market_price.label("price"),
            func.row_number().over(
                partition_by=Transaction.outcome_id,
                order_by=Transaction.timestamp.desc(),
            ).label("rn"),
        )
        .where(
            and_(
                Transaction.outcome_id.in_(all_outcome_ids),
                Transaction.timestamp <= cutoff,
            )
        )
        .subquery()
    )
    p_stmt = select(sub.c.outcome_id, sub.c.price).where(sub.c.rn == 1)
    p_res = await db.execute(p_stmt)
    prices_then: Dict[int, float] = {
        row[0]: float(row[1]) for row in p_res.all() if row[1] and float(row[1]) > 0
    }

    # 3) 计算每个 outcome 的当前价 + 变化
    results: List[MoverItem] = []
    for m in markets:
        outcomes = list(m.outcomes)
        n = len(outcomes)
        if n < 2:
            continue
        shares_list = _shares_to_floats(outcomes)
        b = float(m.liquidity_b)
        for i, o in enumerate(outcomes):
            cur = float(quantize_price(get_current_price(shares_list, i, b)))
            then = prices_then.get(o.id, 1.0 / n)  # 无历史则用初始等概率
            if then <= 0:
                continue
            change_pct = round((cur - then) / then * 100, 2)
            results.append(MoverItem(
                market_id=int(m.id),
                market_title=str(m.title),
                outcome_id=int(o.id),
                outcome_label=str(o.label),
                price_now=quantize_price(cur),
                price_then=quantize_price(then),
                change_pct=change_pct,
            ))

    # 4) 按 |change_pct| 倒序，取 top N（前端再切分涨/跌）
    results.sort(key=lambda x: abs(x.change_pct), reverse=True)
    return results[:limit]
