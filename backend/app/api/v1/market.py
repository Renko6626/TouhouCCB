from datetime import datetime, timezone
from typing import List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.services.realtime import BROKER
from app.core.database import get_async_session
from app.core.users import current_active_user, current_superuser
from app.models.base import User, Market, Outcome, Position, Transaction
from app.schemas.market import (
    MarketCreate,
    TradeRequest,
    TradeResponse,
    SettleRequest,
    ResolveRequest,
    SettleResult,
    MarketDetailRead,
    OutcomeQuoteRead,
    QuoteRequest,
    QuoteResponse,
    LeaderboardItem,
    MarketTradeRead,
)
from app.services.lmsr import calculate_lmsr_cost, get_current_price

router = APIRouter()

SELL_FEE_RATE = 0.00


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


def _build_prices_from_shares(outcomes: List[Outcome], shares_list: List[float], b: float) -> List[Dict[str, Any]]:
    out = []
    for i, o in enumerate(outcomes):
        p = get_current_price(shares_list, i, b)
        out.append({
            "id": o.id,
            "label": o.label,
            "shares": float(shares_list[i]),
            "current_price": round(float(p), 6),
        })
    return out


def _require_trading(market: Market):
    if market.status != "trading":
        raise HTTPException(status_code=400, detail="市场当前不可交易")


def _require_halt(market: Market):
    if market.status != "halt":
        raise HTTPException(status_code=400, detail="市场当前不在熔断期，无法结算")


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
        status="trading",
    )
    db.add(new_market)
    await db.flush()

    for label in data.outcomes:
        db.add(Outcome(market_id=new_market.id, label=label, total_shares=0.0))

    await db.commit()
    return {
        "status": "success",
        "market_id": new_market.id,
        "title": new_market.title,
        "outcomes": data.outcomes,
        "created_by": admin.username,
    }


@router.post("/{market_id}/close", summary="关闭市场交易（仅限管理员）")
async def close_market(
    market_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    # 用事务 + 锁 market 行，避免 close 与 settle 互相打架
    async with db.begin():
        market = await _lock_market(db, market_id)
        if market.status == "settled":
            raise HTTPException(status_code=400, detail="市场已结算，无法熔断")
        market.status = "halt"
    return {"message": f"市场 {market.title} 已停止交易（熔断）"}


@router.post("/{market_id}/settle", summary="结算市场（仅限管理员）")
async def settle_market(
    market_id: int,
    req: SettleRequest,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    """
    结算：赢家兑付 1.00/张，输家归零；兑付按“卖出”收 1% 手续费。
    多进程安全：事务内锁 market + outcomes + positions + users。
    """
    async with db.begin():
        market = await _lock_market(db, market_id)
        _require_halt(market)

        outcomes = await _lock_outcomes_for_market(db, market_id)
        outcome_ids = [o.id for o in outcomes]
        if req.winning_outcome_id not in outcome_ids:
            raise HTTPException(status_code=400, detail="winning_outcome_id 不属于该市场")

        # 标记结算
        market.status = "settled"

        # 锁住所有相关仓位（这里假设 Position 有 outcome_id 外键）
        pos_res = await db.execute(
            select(Position)
            .where(Position.outcome_id.in_(outcome_ids))
            .with_for_update()
        )
        positions = pos_res.scalars().all()

        # 批量锁住相关用户
        user_ids = list({p.user_id for p in positions})
        users = {}
        if user_ids:
            user_res = await db.execute(
                select(User).where(User.id.in_(user_ids)).with_for_update()
            )
            users = {u.id: u for u in user_res.scalars().all()}

        for p in positions:
            amt = float(p.amount)
            if amt <= 0:
                continue
            u = users.get(p.user_id)
            if not u:
                continue

            if p.outcome_id == req.winning_outcome_id:
                gross = amt * 1.0
                fee = gross * SELL_FEE_RATE
                net = gross - fee
                u.cash += net

                db.add(Transaction(
                    user_id=u.id,
                    outcome_id=req.winning_outcome_id,
                    type="settle_win",
                    shares=amt,
                    price=1.0,
                    cost=-net,  # 负数表示现金流入（沿用你 sell 的风格）
                ))
            else:
                db.add(Transaction(
                    user_id=u.id,
                    outcome_id=p.outcome_id,
                    type="settle_lose",
                    shares=amt,
                    price=0.0,
                    cost=0.0,
                ))

            p.amount = 0.0

    return {"message": f"市场已结算：赢家 outcome_id={req.winning_outcome_id}（by {admin.username}）"}


# ==========================================
# 公共接口
# ==========================================

@router.get("/list", summary="获取所有活跃市场")
async def list_markets(db: AsyncSession = Depends(get_async_session)):
    res = await db.execute(
        select(Market)
        .where(Market.status == "trading")
        .options(selectinload(Market.outcomes))
        .order_by(Market.created_at.desc())
    )
    markets = res.scalars().all()

    output = []
    for m in markets:
        outcomes = list(m.outcomes)
        shares_list = [float(o.total_shares) for o in outcomes]
        output.append({
            "id": m.id,
            "title": m.title,
            "description": m.description,
            "liquidity_b": float(m.liquidity_b),
            "status": m.status,
            "outcomes": _build_prices_from_shares(outcomes, shares_list, float(m.liquidity_b)),
        })
    return output


@router.get("/{market_id}", response_model=MarketDetailRead, summary="获取市场详情")
async def get_market_detail(
    market_id: int,
    db: AsyncSession = Depends(get_async_session),
):
    # 1) Market
    market = await db.get(Market, market_id)
    if not market:
        raise HTTPException(status_code=404, detail="市场不存在")

    # 2) Outcomes（显式查，避免 async 下 relationship 懒加载踩坑）
    o_res = await db.execute(
        select(Outcome)
        .where(Outcome.market_id == market.id)
        .order_by(Outcome.id.asc())
    )
    outcomes = o_res.scalars().all()
    if not outcomes:
        raise HTTPException(status_code=400, detail="市场选项异常：无 outcomes")

    shares_list = [float(o.total_shares) for o in outcomes]
    b = float(market.liquidity_b)

    out_reads: list[OutcomeQuoteRead] = []
    for i, o in enumerate(outcomes):
        price = float(get_current_price(shares_list, i, b))
        is_winner = None
        if getattr(market, "winning_outcome_id", None) is not None:
            is_winner = (int(market.winning_outcome_id) == int(o.id))

        out_reads.append(
            OutcomeQuoteRead(
                id=int(o.id),
                label=str(o.label),
                total_shares=float(o.total_shares),
                current_price=round(price, 6),
                payout=getattr(o, "payout", None),
                is_winner=is_winner,
            )
        )

    # 3) last_trade_at（兼容 Transaction 是否有 market_id）
    last_trade_at = None
    if hasattr(Transaction, "market_id"):
        tx_stmt = (
            select(Transaction.timestamp)
            .where(Transaction.market_id == market.id)
            .order_by(Transaction.timestamp.desc())
            .limit(1)
        )
    else:
        # fallback：通过 Outcome.market_id 过滤
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
    """
    多进程安全买入（MySQL 行锁 FOR UPDATE）：
    锁顺序：Outcome -> Market -> Outcomes(同市场全部) -> User -> Position
    """
    shares = float(req.shares)
    if shares <= 0:
        raise HTTPException(status_code=422, detail="shares 必须为正数")

    async with db.begin():
        # 1) 锁目标 outcome（拿 market_id）
        outcome = await _lock_outcome(db, int(req.outcome_id))

        # 2) 锁 market 并检查状态
        market = await _lock_market(db, int(outcome.market_id))
        _require_trading(market)

        # 3) 锁该市场所有 outcomes（LMSR 状态）
        all_outcomes = await _lock_outcomes_for_market(db, int(market.id))
        target_idx = next((i for i, o in enumerate(all_outcomes) if o.id == outcome.id), None)
        if target_idx is None:
            raise HTTPException(status_code=400, detail="选项不属于该市场（数据异常）")

        # 4) 锁 user（防并发扣款）
        locked_user = await _lock_user(db, int(user.id))

        # 5) LMSR 成本差
        b = float(market.liquidity_b)
        old_q = [float(o.total_shares) for o in all_outcomes]
        old_cost = float(calculate_lmsr_cost(old_q, b))

        new_q = list(old_q)
        new_q[target_idx] += shares
        new_cost = float(calculate_lmsr_cost(new_q, b))

        pay = new_cost - old_cost
        # 由于浮点误差，可能出现极小负数
        if pay <= 1e-12:
            raise HTTPException(status_code=400, detail="订单异常：成本不应为非正")

        if float(locked_user.cash) + 1e-12 < pay:
            raise HTTPException(status_code=400, detail="现金不足")

        # 6) 更新用户现金与市场份额
        locked_user.cash = float(locked_user.cash) - pay
        all_outcomes[target_idx].total_shares = new_q[target_idx]

        # 7) 锁 / 更新持仓
        pos_res = await db.execute(
            select(Position)
            .where(Position.user_id == locked_user.id, Position.outcome_id == outcome.id)
            .with_for_update()
        )
        position = pos_res.scalars().first()
        if not position:
            position = Position(user_id=locked_user.id, outcome_id=outcome.id, amount=0.0)
            db.add(position)
        position.amount = float(position.amount) + shares

        # 8) 交易记录（用于价格曲线 / K线）
        avg_price = pay / shares
        fee = 0.0
        gross = pay  # buy 没手续费：gross=pay

        db.add(Transaction(
            user_id=locked_user.id,
            outcome_id=outcome.id,
            type="buy",
            shares=shares,

            # ✅ 现金流：buy 为正支出（沿用你原记法）
            cost=float(pay),

            # ✅ 图表字段（若你已升级 Transaction）
            price=float(avg_price),   # 手续费前单价（buy 就是 avg）
            gross=float(gross),
            fee=float(fee),
        ))

    await BROKER.publish(
        market.id,
        "trade",
        {
            "trade": {
                "type": "buy",
                "outcome_id": int(outcome.id),
                "shares": float(req.shares),
                "price": float(avg_price),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        }
    )

    # 注意：出事务后 outcome / locked_user 仍是 ORM 对象，但字段值已更新
    return {
        "shares": shares,
        "cost": round(float(pay), 2),
        "new_cash": round(float(locked_user.cash), 2),
        "message": f"成功买入 {shares:g} 张 {outcome.label}（均价≈{avg_price:.6f}）",
    }


@router.post("/sell", response_model=TradeResponse, summary="卖出胜券")
async def sell_shares(
    req: TradeRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    """
    多进程安全卖出（MySQL 行锁 FOR UPDATE）：
    锁顺序：Position -> Outcome -> Market -> Outcomes(同市场全部) -> User
    """
    shares = float(req.shares)
    if shares <= 0:
        raise HTTPException(status_code=422, detail="shares 必须为正数")

    async with db.begin():
        # 1) 锁 position 防并发卖出导致负仓
        pos_res = await db.execute(
            select(Position)
            .where(Position.user_id == int(user.id), Position.outcome_id == int(req.outcome_id))
            .with_for_update()
        )
        position = pos_res.scalars().first()
        if not position or float(position.amount) + 1e-12 < shares:
            raise HTTPException(status_code=400, detail="持仓不足")

        # 2) 锁 outcome / market
        outcome = await _lock_outcome(db, int(req.outcome_id))
        market = await _lock_market(db, int(outcome.market_id))
        _require_trading(market)

        # 3) 锁该市场所有 outcomes（LMSR 状态）
        all_outcomes = await _lock_outcomes_for_market(db, int(market.id))
        target_idx = next((i for i, o in enumerate(all_outcomes) if o.id == outcome.id), None)
        if target_idx is None:
            raise HTTPException(status_code=400, detail="选项不属于该市场（数据异常）")

        # 4) 锁 user（防并发加钱/扣钱）
        locked_user = await _lock_user(db, int(user.id))

        # 5) LMSR：卖出收益 = old_cost - new_cost
        b = float(market.liquidity_b)
        old_q = [float(o.total_shares) for o in all_outcomes]
        if old_q[target_idx] + 1e-12 < shares:
            raise HTTPException(status_code=400, detail="市场总份额不足（异常状态）")

        old_cost = float(calculate_lmsr_cost(old_q, b))

        new_q = list(old_q)
        new_q[target_idx] -= shares
        new_cost = float(calculate_lmsr_cost(new_q, b))

        proceeds = old_cost - new_cost
        if proceeds < -1e-12:
            raise HTTPException(status_code=400, detail="订单异常：收益不应为负")
        proceeds = max(0.0, proceeds)  # 浮点抖动修正

        fee = proceeds * SELL_FEE_RATE
        net = proceeds - fee

        # 6) 更新状态
        locked_user.cash = float(locked_user.cash) + net
        all_outcomes[target_idx].total_shares = new_q[target_idx]
        position.amount = float(position.amount) - shares

        # 7) 交易记录（用于价格曲线 / K线）
        avg_price = (proceeds / shares) if shares > 0 else 0.0

        db.add(Transaction(
            user_id=locked_user.id,
            outcome_id=outcome.id,
            type="sell",
            shares=shares,

            # ✅ 现金流：sell 为负数（表示现金流入）
            cost=-float(net),

            # ✅ 图表字段：K线应使用手续费前价格
            price=float(avg_price),
            gross=float(proceeds),
            fee=float(fee),
        ))
    await BROKER.publish(
        market.id,
        "trade",
        {
            "trade": {
                "type": "sell",
                "outcome_id": int(outcome.id),
                "shares": float(req.shares),
                "price": float(proceeds / shares if shares > 0 else 0.0),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        }
    )

    return {
        "shares": shares,
        "cost": round(-float(net), 2),
        "new_cash": round(float(locked_user.cash), 2),
        "message": f"卖出成功，获得 {net:.2f}（手续费 {fee:.2f}，均价≈{avg_price:.6f}）",
    }


@router.post(
    "/{market_id}/resolve",
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
    """
    结算流程（幂等 + 强一致）：
    1) 锁 market 行
    2) 锁该 market 的 outcomes 行（与交易互斥）
    3) 验证 winning_outcome_id 属于该 market
    4) 若已 settled：直接返回（幂等）
    5) 锁所有相关 positions（通过 join outcome.market_id 过滤）
    6) 对赢家 outcome：按 amount * payout 发钱，清零仓位
       对输家 outcome：清零仓位
    7) 写入 Transaction(type="settle") 作为审计
    8) 更新 market.status/settled_at/winning_outcome_id/settled_by_user_id
    """
    payout_unit = float(req.payout)
    if payout_unit < 0:
        raise HTTPException(status_code=422, detail="payout 必须 >= 0")

    async with db.begin():
        # 1) 锁 market
        m_res = await db.execute(
            select(Market).where(Market.id == market_id).with_for_update()
        )
        market = m_res.scalars().first()
        if not market:
            raise HTTPException(status_code=404, detail="市场不存在")

        # 幂等：如果已结算，直接返回
        if market.status == "settled":
            if market.winning_outcome_id is None or market.settled_at is None:
                # 数据异常，但仍然告诉管理员
                raise HTTPException(status_code=500, detail="市场已结算但结算字段缺失（数据异常）")
            return SettleResult(
                market_id=market.id,
                status=market.status,
                winning_outcome_id=int(market.winning_outcome_id),
                settled_at=market.settled_at,
                total_payout=0.0,
                settled_positions=0,
            )

        # 2) 锁 outcomes（与 buy/sell 的 outcomes 锁一致，从而互斥）
        o_res = await db.execute(
            select(Outcome)
            .where(Outcome.market_id == market.id)
            .order_by(Outcome.id.asc())
            .with_for_update()
        )
        outcomes = o_res.scalars().all()
        if len(outcomes) < 2:
            raise HTTPException(status_code=400, detail="市场选项数量异常")

        # 3) 验证赢家属于该市场
        winning = next((o for o in outcomes if o.id == req.winning_outcome_id), None)
        if not winning:
            raise HTTPException(status_code=400, detail="winning_outcome_id 不属于该市场")

        # 建 outcome_id -> is_winner
        is_winner = {o.id: (o.id == winning.id) for o in outcomes}

        # 4) 锁相关 positions（只锁这个 market 下的持仓）
        # 通过 Outcome.market_id 过滤，避免锁全表
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

        # 5) 批量计算兑付：赢家 amount * payout_unit
        # 这里我们需要给用户加钱，所以要锁用户行；否则并发改 cash 会出问题
        # 为了不 N+1，我们先聚合 payout_by_user
        payout_by_user: dict[int, float] = {}
        settled_positions = 0

        for pos in positions:
            amt = float(pos.amount)
            if amt <= 0:
                continue
            settled_positions += 1

            if is_winner.get(pos.outcome_id, False):
                payout_amt = amt * payout_unit
                if payout_amt > 0:
                    payout_by_user[pos.user_id] = payout_by_user.get(pos.user_id, 0.0) + payout_amt

            # 无论输赢，仓位清零（最简单规则）
            pos.amount = 0.0

        # 6) 给用户发钱（逐个锁 user 行并更新 cash，同时写 settle 交易记录）
        total_payout = 0.0
        now = datetime.now(timezone.utc)

        for uid, pay in payout_by_user.items():
            if pay <= 0:
                continue

            u_res = await db.execute(
                select(User).where(User.id == uid).with_for_update()
            )
            u = u_res.scalars().first()
            if not u:
                # 极端情况：用户被删了？这里按你的业务决定。这里我选择报错回滚，避免账不平。
                raise HTTPException(status_code=500, detail=f"用户 {uid} 不存在，无法结算（已回滚）")

            u.cash = float(u.cash) + float(pay)
            total_payout += float(pay)

            db.add(Transaction(
                user_id=u.id,
                market_id=market.id,
                outcome_id=winning.id,
                type="settle",
                shares=0.0,
                gross=float(pay),
                fee=0.0,
                price=0.0,
                cost=-float(pay),   # ✅ 负数表示现金流入（与你 sell 一致）
                timestamp=now,
            ))

        # 7) 写 outcome payout（可选）
        for o in outcomes:
            o.payout = payout_unit if o.id == winning.id else 0.0

        # 8) 更新市场状态
        market.status = "settled"
        market.winning_outcome_id = winning.id
        market.settled_at = now
        market.settled_by_user_id = admin.id
    await BROKER.publish(
        market.id,
        "market_status",
        {
            "status": "settled",
            "winning_outcome_id": int(winning.id),
            "settled_at": now.isoformat(),
        }
    )
    # 事务结束自动提交
    return SettleResult(
        market_id=market.id,
        status=market.status,
        winning_outcome_id=int(market.winning_outcome_id),
        settled_at=market.settled_at,
        total_payout=round(float(total_payout), 6),
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

    outcomes_res = await db.execute(
        select(Outcome).where(Outcome.market_id == market.id).order_by(Outcome.id)
    )
    outcomes = outcomes_res.scalars().all()

    idx = next(i for i,o in enumerate(outcomes) if o.id == outcome.id)
    b = float(market.liquidity_b)
    shares = float(req.shares)

    old_q = [float(o.total_shares) for o in outcomes]
    old_cost = calculate_lmsr_cost(old_q, b)

    new_q = list(old_q)
    if req.side == "buy":
        new_q[idx] += shares
        new_cost = calculate_lmsr_cost(new_q, b)
        gross = new_cost - old_cost
        fee = 0.0
        net = gross
    else:
        new_q[idx] -= shares
        new_cost = calculate_lmsr_cost(new_q, b)
        gross = old_cost - new_cost
        fee = gross * SELL_FEE_RATE
        net = gross - fee

    avg_price = gross / shares if shares > 0 else 0.0
    after_prices = _build_prices_from_shares(outcomes, new_q, b)

    return QuoteResponse(
        outcome_id=req.outcome_id,
        side=req.side,
        shares=shares,
        avg_price=round(avg_price, 6),
        gross=round(gross, 6),
        fee=round(fee, 6),
        net=round(net, 6),
        after_prices=after_prices,
    )
@router.get(
    "/{market_id}/trades",
    response_model=List[MarketTradeRead],
    summary="市场逐笔成交"
)
async def get_market_trades(
    market_id: int,
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_async_session),
):
    stmt = (
        select(Transaction)
        .join(Outcome, Transaction.outcome_id == Outcome.id)
        .where(Outcome.market_id == market_id)
        .order_by(Transaction.timestamp.desc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    txs = res.scalars().all()

    return [
        MarketTradeRead(
            id=tx.id,
            outcome_id=tx.outcome_id,
            side=tx.type,
            shares=float(tx.shares),
            price=float(tx.price),
            gross=float(tx.gross),
            fee=float(tx.fee),
            timestamp=tx.timestamp,
        )
        for tx in txs
    ]
@router.post("/{market_id}/resume", summary="恢复市场交易（仅管理员）")
async def resume_market(
    market_id: int,
    admin: User = Depends(current_superuser),
    db: AsyncSession = Depends(get_async_session),
):
    async with db.begin():
        market = await _lock_market(db, market_id)
        if market.status != "halt":
            raise HTTPException(status_code=400, detail="市场当前不在熔断状态")
        market.status = "trading"

    await BROKER.publish(
        market.id,
        "market_status",
        {"status": "trading"}
    )
    return {"message": f"市场 {market.title} 已恢复交易"}
from app.schemas.market import LeaderboardItem

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
        net = float(u.cash - u.debt)
        rank = "初入幻想乡"
        if net > 500: rank = "人间之里商人"
        if net > 2000: rank = "命莲寺赞助者"
        if net > 10000: rank = "守矢VIP"
        if net > 50000: rank = "大天狗座上宾"

        items.append(LeaderboardItem(
            user_id=u.id,
            username=u.username,
            net_worth=round(net, 2),
            rank=rank
        ))

    return items
