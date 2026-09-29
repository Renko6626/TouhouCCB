"""统一组合估值（只读：不写库、不推进 ``debt_last_accrued_at``）。

两种净值，边界不得混用：

- ``display_equity = cash + MTM_lmsr + MTM_fx − D_effective``：展示口径，
  MTM 含 HALT / paused 资产（账面价，不关心能否变现）。
- ``liquidation_equity = cash + Σ L_group − D_effective``：风控口径，
  每个 LMSR market / FX pair 用各自真实组清算算法，不可执行资产 ``L = 0``。

借款额度、下单准入、强平触发/停止只用 ``liquidation_equity``。``D_effective``
调用 ``loan_service.pending_debt``（与计息同源），不落库。

批量读取固定 5 条 SELECT（与用户数无关）：User → Position+Outcome+Market →
Outcome（组内全量 q）→ FxWallet+Pair → sell_fee_rate 配置（仅 LMSR 持仓时）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Market, Outcome, Position, User
from app.models.fx import FxPair, FxWallet
from app.services import site_config
from app.services.credit.fx_quote import FxPairSnapshot, quote_fx_group
from app.services.credit.keys import GroupKey, sort_groups_by_liquidation
from app.services.credit.lmsr_quote import (
    BLOCKED_NO_OUTCOMES,
    OutcomeSnapshot,
    quote_lmsr_group,
)
from app.services.fx.amm import marginal_price
from app.services.lmsr import get_current_price
from app.services.loan_service import pending_debt

ZERO = Decimal("0")
ONE = Decimal("1")
Q6 = Decimal("0.000001")
SELL_FEE_RATE_KEY = "sell_fee_rate"


@dataclass(frozen=True)
class GroupLiquidation:
    key: GroupKey
    value: Decimal
    executable: bool
    blocked_reason: str | None


@dataclass(frozen=True)
class AccountValuation:
    user_id: int
    cash: Decimal
    debt_persisted: Decimal
    debt_effective: Decimal
    mtm_lmsr: Decimal
    mtm_fx: Decimal
    display_equity: Decimal
    liquidation_equity: Decimal
    groups: tuple[GroupLiquidation, ...]
    economic_version: int


def _as_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"{name} 不是合法 Decimal: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} 必须是有限 Decimal: {value!r}")
    return parsed


async def _account_valuations(
    session: AsyncSession,
    user_ids: Sequence[int],
    *,
    daily_rate: Decimal,
    now: datetime,
    lock: bool,
) -> dict[int, AccountValuation]:
    ids = list(dict.fromkeys(int(uid) for uid in user_ids))
    if not ids:
        return {}

    user_stmt = select(User).where(User.id.in_(ids))
    if lock:
        user_stmt = user_stmt.with_for_update()
    users = list((await session.execute(user_stmt)).scalars().all())
    if not users:
        return {}

    # User → Position+Outcome+Market（显式 join，避免 raise_on_sql 关系触发懒加载）
    pos_rows = (await session.execute(
        select(
            Position.user_id,
            Position.outcome_id,
            Position.amount,
            Outcome.market_id,
            Market.status,
            Market.closes_at,
            Market.liquidity_b,
        )
        .join(Outcome, Outcome.id == Position.outcome_id)
        .join(Market, Market.id == Outcome.market_id)
        .where(Position.user_id.in_(ids), Position.amount > ZERO)
    )).all()

    market_meta: dict[int, tuple[str, datetime | None, float]] = {}
    positions_by_user: dict[int, list[tuple[int, int, Decimal]]] = {}
    for uid, outcome_id, amount, market_id, status, closes_at, liquidity_b in pos_rows:
        uid, outcome_id, market_id = int(uid), int(outcome_id), int(market_id)
        market_meta[market_id] = (status, closes_at, float(liquidity_b))
        positions_by_user.setdefault(uid, []).append(
            (market_id, outcome_id, Decimal(amount)),
        )

    outcomes_by_market: dict[int, list[tuple[int, Decimal]]] = {}
    if market_meta:
        outcome_rows = (await session.execute(
            select(Outcome.id, Outcome.market_id, Outcome.total_shares)
            .where(Outcome.market_id.in_(list(market_meta)))
            .order_by(Outcome.market_id, Outcome.id)
        )).all()
        for outcome_id, market_id, total_shares in outcome_rows:
            outcomes_by_market.setdefault(int(market_id), []).append(
                (int(outcome_id), Decimal(total_shares)),
            )

    wallet_rows = (await session.execute(
        select(
            FxWallet.user_id,
            FxWallet.pair_id,
            FxWallet.foreign_amount,
            FxPair.status,
            FxPair.reduce_only,
            FxPair.gold_reserve,
            FxPair.foreign_reserve,
            FxPair.sell_fee_rate,
        )
        .join(FxPair, FxPair.id == FxWallet.pair_id)
        .where(FxWallet.user_id.in_(ids), FxWallet.foreign_amount > ZERO)
    )).all()
    wallets_by_user: dict[int, list[tuple[FxPairSnapshot, Decimal]]] = {}
    for (uid, pair_id, foreign_amount, status, reduce_only,
         gold_reserve, foreign_reserve, sell_fee_rate) in wallet_rows:
        wallets_by_user.setdefault(int(uid), []).append((
            FxPairSnapshot(
                pair_id=int(pair_id),
                status=status,
                reduce_only=bool(reduce_only),
                gold_reserve=Decimal(gold_reserve),
                foreign_reserve=Decimal(foreign_reserve),
                sell_fee_rate=Decimal(sell_fee_rate),
            ),
            Decimal(foreign_amount),
        ))

    lmsr_fee_rate = ZERO
    if market_meta:
        lmsr_fee_rate = await site_config.get_decimal_or(
            session, SELL_FEE_RATE_KEY, ZERO,
        )

    result: dict[int, AccountValuation] = {}
    for user in users:
        uid = int(user.id)
        cash = Decimal(user.cash)
        debt = Decimal(user.debt)
        debt_effective = pending_debt(user, daily_rate, now)
        mtm_lmsr = ZERO
        mtm_fx = ZERO
        groups: list[GroupLiquidation] = []

        by_market: dict[int, list[tuple[int, Decimal]]] = {}
        for market_id, outcome_id, amount in positions_by_user.get(uid, []):
            by_market.setdefault(market_id, []).append((outcome_id, amount))

        for market_id, market_positions in by_market.items():
            status, closes_at, liquidity_b = market_meta[market_id]
            market_outcomes = outcomes_by_market.get(market_id, [])
            if not market_outcomes:
                groups.append(GroupLiquidation(
                    key=GroupKey("lmsr", market_id),
                    value=ZERO,
                    executable=False,
                    blocked_reason=BLOCKED_NO_OUTCOMES,
                ))
                continue
            shares_list = [float(shares) for _, shares in market_outcomes]
            index_of = {oid: i for i, (oid, _) in enumerate(market_outcomes)}
            for outcome_id, amount in market_positions:
                idx = index_of.get(outcome_id)
                if idx is None:
                    continue
                price = Decimal(str(get_current_price(shares_list, idx, liquidity_b)))
                mtm_lmsr += (amount * price).quantize(Q6)
            quote = quote_lmsr_group(
                [
                    OutcomeSnapshot(
                        outcome_id=oid, total_shares=shares,
                        status=status, closes_at=closes_at,
                    )
                    for oid, shares in market_outcomes
                ],
                dict(market_positions),
                market_id=market_id,
                b=liquidity_b,
                fee_rate=lmsr_fee_rate,
                mode="full",
                partial_pct=ONE,
            )
            groups.append(GroupLiquidation(
                key=GroupKey("lmsr", market_id),
                value=quote.net,
                executable=quote.blocked_reason is None,
                blocked_reason=quote.blocked_reason,
            ))

        for pair, foreign_amount in wallets_by_user.get(uid, []):
            try:
                price = marginal_price(pair.gold_reserve, pair.foreign_reserve)
            except ValueError:
                price = ZERO
            mtm_fx += (foreign_amount * price).quantize(Q6)
            quote = quote_fx_group(
                pair,
                foreign_amount=foreign_amount,
                mode="full",
                partial_pct=ONE,
            )
            groups.append(GroupLiquidation(
                key=GroupKey("fx", pair.pair_id),
                value=quote.gold_out,
                executable=quote.blocked_reason is None,
                blocked_reason=quote.blocked_reason,
            ))

        holdings = sum((group.value for group in groups), ZERO)
        result[uid] = AccountValuation(
            user_id=uid,
            cash=cash,
            debt_persisted=debt,
            debt_effective=debt_effective,
            mtm_lmsr=mtm_lmsr,
            mtm_fx=mtm_fx,
            display_equity=(cash + mtm_lmsr + mtm_fx - debt_effective).quantize(Q6),
            liquidation_equity=(cash + holdings - debt_effective).quantize(Q6),
            groups=tuple(sort_groups_by_liquidation(groups)),
            economic_version=int(user.economic_version or 0),
        )
    return result


async def value_users_batch(
    session: AsyncSession,
    user_ids: Sequence[int],
    *,
    daily_rate: Decimal,
) -> dict[int, AccountValuation]:
    """批量估值；返回 dict 只含实际存在的 user_id（缺失用户由调用方处理）。"""
    return await _account_valuations(
        session, user_ids,
        daily_rate=_as_decimal(daily_rate, "daily_rate"),
        now=datetime.now(timezone.utc),
        lock=False,
    )


async def value_user_detailed(
    session: AsyncSession,
    user_id: int,
    *,
    daily_rate: Decimal,
    lock: bool = False,
) -> AccountValuation:
    """单用户估值；``lock=True`` 时对 user 行 ``FOR UPDATE``（调用方持有事务）。"""
    uid = int(user_id)
    result = await _account_valuations(
        session, [uid],
        daily_rate=_as_decimal(daily_rate, "daily_rate"),
        now=datetime.now(timezone.utc),
        lock=lock,
    )
    if uid not in result:
        raise ValueError(f"user not found: {uid}")
    return result[uid]
