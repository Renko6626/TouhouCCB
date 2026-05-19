"""持仓估值的两套口径。

本项目对"持仓估值"有两种语义，对应不同场景：

- **LCV** (Liquidation Value, "立即清算价值")：`compute_users_holdings_value`
  按 LMSR cost diff 算「全部卖出能拿到多少」，含滑点 + 扣卖出 fee。**保守**。
  用于：强平判定、margin 计算、借款额度上限、内部分析（/admin/wealth）。
  **HALT/RESOLVED 持仓不计入**（不可变现 → 立即清算价值为 0），防止 fake collateral。

- **MTM** (Mark-to-Market, "账面瞬时估值")：`compute_users_holdings_value_mtm`
  按瞬时价 × 数量算（LMSR 边际价 × position.amount），**不含滑点不扣 fee**。
  直观，符合用户对"我有多少钱"的认知。
  用于：/user/summary 主显示、排行榜排序、UI 显示净值。
  **HALT/RESOLVED 持仓正常计入**（按瞬时价算账面估值，不关心能否立即变现）。
  避免临时 HALT 让用户账面归零体感像突然爆仓。

两套对 HALT 的不同处理是有意为之的：MTM 是"账面"，LCV 是"流动性"。两者对大持仓
用户差距可达 20%+（LMSR `b=100` 量级时单笔滑点 10% 量级）。**必须分场景使用**，
不能交叉，否则会出现"借款看到 NW=500、Portfolio 看到 350"的分裂体感。
详见 docs/holdings-value-semantics.md。

不进 buy/sell hot path；调用方一般是榜单/统计/估值接口。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.base import Market, MarketStatus, Outcome, Position
from app.services.lmsr import calculate_lmsr_cost, get_current_price, quantize_cost

ZERO = Decimal("0")
ONE = Decimal("1")

# 延迟导入避免循环依赖：services 层不能在模块级 import api 层。
# _get_sell_fee_rate() 在首次调用时读取并缓存。
_CACHED_SELL_FEE_RATE: Optional[Decimal] = None


def _get_sell_fee_rate() -> Decimal:
    global _CACHED_SELL_FEE_RATE
    if _CACHED_SELL_FEE_RATE is None:
        from app.api.v1.market import SELL_FEE_RATE  # noqa: PLC0415
        _CACHED_SELL_FEE_RATE = SELL_FEE_RATE
    return _CACHED_SELL_FEE_RATE


_SENTINEL = object()


async def compute_users_holdings_value(
    db: AsyncSession,
    user_ids: Optional[Iterable[int]] = None,
    *,
    sell_fee_rate: Decimal = _SENTINEL,  # type: ignore[assignment]
) -> Dict[int, Decimal]:
    """返回 {user_id: 持仓 LMSR 清算价值}。

    口径：对每个 Position，gross = LMSR(cost_before - cost_after_full_sell)，
    持仓价 = gross × (1 - sell_fee_rate)；汇总同一用户全部仓位。

    user_ids=None  → 所有有 amount>0 持仓的用户都算（admin /wealth 用）。
    user_ids=[...] → 只算这部分用户（leaderboard 用）。
    返回 dict 只包含**至少有一笔有效持仓**的 user_id；其他 user_id 调用方按 0 处理。

    sell_fee_rate 默认为项目 SELL_FEE_RATE（"立即清算口径"，即可变现净值）。
    如需 gross/不扣费口径，调用方显式传 sell_fee_rate=Decimal("0")。
    """
    if sell_fee_rate is _SENTINEL:  # type: ignore[comparison-overlap]
        sell_fee_rate = _get_sell_fee_rate()
    user_ids_list: Optional[List[int]] = None
    if user_ids is not None:
        user_ids_list = list(user_ids)
        if not user_ids_list:
            return {}

    pos_stmt = (
        select(Position)
        .where(Position.amount > 0)
        .options(selectinload(Position.outcome).selectinload(Outcome.market))
    )
    if user_ids_list is not None:
        pos_stmt = pos_stmt.where(Position.user_id.in_(user_ids_list))

    positions: List[Position] = (await db.execute(pos_stmt)).scalars().all()
    if not positions:
        return {}

    positions_by_user: Dict[int, List[Position]] = {}
    for p in positions:
        positions_by_user.setdefault(p.user_id, []).append(p)

    market_ids = {p.outcome.market_id for p in positions}
    outcomes_by_market: Dict[int, List[Outcome]] = {}
    if market_ids:
        all_outcomes = (await db.execute(
            select(Outcome)
            .where(Outcome.market_id.in_(market_ids))
            .order_by(Outcome.market_id, Outcome.id)
        )).scalars().all()
        for o in all_outcomes:
            outcomes_by_market.setdefault(o.market_id, []).append(o)

    fee_factor = ONE - sell_fee_rate
    result: Dict[int, Decimal] = {}
    for uid, user_positions in positions_by_user.items():
        total = ZERO
        for pos in user_positions:
            market: Market = pos.outcome.market
            # 只算可交易市场的持仓：HALT/RESOLVED 市场的份额无法立即变现，
            # 不应计入抵押估值/margin。这与 liquidation_service.py 的"跳过非
            # TRADING 市场"约定一致，避免 fake collateral 风险。
            if market.status != MarketStatus.TRADING:
                continue
            ctx_outcomes = outcomes_by_market.get(market.id, [])
            if not ctx_outcomes:
                continue
            shares_list = [float(o.total_shares) for o in ctx_outcomes]
            idx = next(
                (i for i, o in enumerate(ctx_outcomes) if o.id == pos.outcome_id),
                None,
            )
            if idx is None:
                continue
            b = float(market.liquidity_b)
            old_cost = calculate_lmsr_cost(shares_list, b)
            after_sell = list(shares_list)
            after_sell[idx] -= float(pos.amount)
            new_cost = calculate_lmsr_cost(after_sell, b)
            gross = quantize_cost(old_cost - new_cost)
            total += quantize_cost(gross * fee_factor)
        result[uid] = total
    return result


async def compute_users_holdings_value_mtm(
    db: AsyncSession,
    user_ids: Optional[Iterable[int]] = None,
) -> Dict[int, Decimal]:
    """返回 {user_id: 持仓 MTM 价值} = Σ(amount × LMSR 边际价)。

    MTM (mark-to-market)：按当前 LMSR 边际价乘持仓数量算账面估值。
    **不含滑点、不扣手续费**，等价于"假设按当前瞬时价交易小量股"。
    通常 > LCV，差距 ≈ 滑点 + sell_fee。

    用于：/user/summary 主显示、排行榜排序、unrealized_pnl 计算。
    **不要用于强平/借款额度判定**——那些场景必须用 compute_users_holdings_value (LCV)
    才能保证保守安全。

    实现参考已废弃的 loan.py:_holdings_value，但批量化避免 N+1 query。
    """
    user_ids_list: Optional[List[int]] = None
    if user_ids is not None:
        user_ids_list = list(user_ids)
        if not user_ids_list:
            return {}

    pos_stmt = (
        select(Position)
        .where(Position.amount > 0)
        .options(selectinload(Position.outcome).selectinload(Outcome.market))
    )
    if user_ids_list is not None:
        pos_stmt = pos_stmt.where(Position.user_id.in_(user_ids_list))

    positions: List[Position] = (await db.execute(pos_stmt)).scalars().all()
    if not positions:
        return {}

    positions_by_user: Dict[int, List[Position]] = {}
    for p in positions:
        positions_by_user.setdefault(p.user_id, []).append(p)

    market_ids = {p.outcome.market_id for p in positions}
    outcomes_by_market: Dict[int, List[Outcome]] = {}
    if market_ids:
        all_outcomes = (await db.execute(
            select(Outcome)
            .where(Outcome.market_id.in_(market_ids))
            .order_by(Outcome.market_id, Outcome.id)
        )).scalars().all()
        for o in all_outcomes:
            outcomes_by_market.setdefault(o.market_id, []).append(o)

    result: Dict[int, Decimal] = {}
    for uid, user_positions in positions_by_user.items():
        total = ZERO
        for pos in user_positions:
            market: Market = pos.outcome.market
            # MTM 不过滤 HALT/RESOLVED：账面估值的语义是"按当前 LMSR 瞬时价
            # 计算"，不关心能否立即变现。否则用户全押 HALT 时账面归零，体感像
            # 突然爆仓。LCV 那边过滤 HALT 是为了 margin/借款额度的保守语义，
            # 两套口径职责不同。详见 docs/holdings-value-semantics.md。
            ctx_outcomes = outcomes_by_market.get(market.id, [])
            if not ctx_outcomes:
                continue
            idx = next(
                (i for i, o in enumerate(ctx_outcomes) if o.id == pos.outcome_id),
                None,
            )
            if idx is None:
                continue
            b = float(market.liquidity_b)
            shares_list = [float(o.total_shares) for o in ctx_outcomes]
            price = Decimal(str(get_current_price(shares_list, idx, b)))
            mtm = (pos.amount * price).quantize(Decimal("0.000001"))
            total += mtm
        result[uid] = total
    return result
