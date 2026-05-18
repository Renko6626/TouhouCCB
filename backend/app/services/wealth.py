"""持仓估值与净值统一口径。

`compute_users_holdings_value` 批量计算指定用户的持仓 LMSR 清算价值（含滑点 +
扣卖出 fee），与 /user/summary 同口径。给 /market/leaderboard、/admin/wealth
等批量场景共用，避免口径分裂。

不进 buy/sell hot path；调用方一般是榜单/统计接口。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.base import Market, Outcome, Position
from app.services.lmsr import calculate_lmsr_cost, quantize_cost

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
