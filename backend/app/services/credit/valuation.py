"""统一组合估值（只读：不写库、不推进任何计息时点）。

两种净值，边界不得混用：

- ``display_equity = cash + MTM_lmsr + MTM_fx − D_effective − 有来源的边际空头债``：
  展示口径，MTM 含 HALT / paused 资产（账面价，不关心能否变现）。边际空头债
  不是可执行回补成本。
- ``liquidation_equity = cash + Σ L_asset − D_effective − K``：风控口径，
  每个 LMSR market / FX pair 用各自真实整组清算算法，不可执行资产 ``L = 0``；
  ``K = Σ quote_buy_exact_out(各 pair 全部含息欠币)``。任一空头无法完整报价时
  ``liquidation_equity`` / ``risk_basis`` 为 ``None``（不得写 0、Infinity/NaN）。

借款额度、下单准入、强平触发/停止只用 ``liquidation_equity``。``D_effective``
调用 ``loan_service.pending_debt``，空头含息欠币调用 ``shorts.pending_short_debt``
（与计息同源），都不落库。

批量读取固定条数与用户数无关（每条都是 ``IN (...)`` 聚合或 join）：
User → Position+Outcome+Market → Outcome（组内全量 q）→ FxWallet+Pair →
FxShortPosition+Pair → sell_fee_rate 配置（仅 LMSR 持仓时）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Literal, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Market, Outcome, Position, User
from app.models.fx import FxPair, FxShortPosition, FxWallet
from app.services import site_config
from app.services.credit import flags as credit_flags
from app.services.credit.fx_quote import (
    BLOCKED_SHORT_QUOTE_FAILED,
    FxPairSnapshot,
    FxShortPairSnapshot,
    quote_fx_group,
    quote_fx_short_group,
)
from app.services.credit.keys import GroupKey, sort_groups_by_liquidation
from app.services.credit.lmsr_quote import (
    BLOCKED_NO_OUTCOMES,
    OutcomeSnapshot,
    quote_lmsr_group,
)
from app.services.fx.amm import marginal_price
from app.services.fx.shorts import pending_short_debt
from app.services.lmsr import get_current_price
from app.services.loan_service import pending_debt

ZERO = Decimal("0")
ONE = Decimal("1")
Q6 = Decimal("0.000001")
SELL_FEE_RATE_KEY = "sell_fee_rate"

#: 估值完整性状态（spec §5.2：未知回补成本必须 blocked，不能与已知区分不开）。
RISK_STATUS_OK = "ok"
RISK_STATUS_BLOCKED = "blocked"

#: 现金用途边界被破坏（0 <= S <= C 不成立），属数据错误，不伪造成已知净值。
REASON_RESTRICTED_EXCEEDS_CASH = "restricted_cash_exceeds_cash"
REASON_INVALID_SHORT_DEBT = "invalid_short_debt"

GroupRole = Literal["asset_sale", "short_cover"]


@dataclass(frozen=True)
class GroupLiquidation:
    """一个可清算组。

    ``role="asset_sale"``：``value`` 是正资产整组净回收（不可执行时为 0）。
    ``role="short_cover"``：``value`` 是**正的回补成本 K**（不是负回收），未知
    回补成本时 ``value is None`` 且 ``executable=False``。动作目的由 ``role``
    显式区分，不把空头成本塞进正回收过滤器（spec §8.1 第 2 步）。
    """

    key: GroupKey
    value: Decimal | None
    executable: bool
    blocked_reason: str | None
    role: GroupRole = "asset_sale"


@dataclass(frozen=True)
class AccountValuation:
    user_id: int
    cash: Decimal
    debt_persisted: Decimal
    debt_effective: Decimal
    mtm_lmsr: Decimal
    mtm_fx: Decimal
    display_equity: Decimal
    liquidation_equity: Decimal | None
    groups: tuple[GroupLiquidation, ...]
    economic_version: int
    # ── WP2b1 空头读模型（C 已含 S，估值不重复相加）──
    short_cover_cost: Decimal | None = None
    short_marginal_debt: Decimal | None = None
    risk_basis: Decimal | None = None
    available_cash: Decimal | None = None
    restricted_cash: Decimal = ZERO
    risk_status: str = RISK_STATUS_OK
    blocked_reason: str | None = None


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

    # 空头（含息欠币 + 锁金）一次批量读取；不逐用户逐 pair 查公共行情。
    short_rows = (await session.execute(
        select(
            FxShortPosition.user_id,
            FxShortPosition.pair_id,
            FxShortPosition.principal_foreign,
            FxShortPosition.interest_foreign,
            FxShortPosition.interest_last_accrued_at,
            FxShortPosition.restricted_gold,
            FxPair.status,
            FxPair.reduce_only,
            FxPair.gold_reserve,
            FxPair.foreign_reserve,
            FxPair.buy_fee_rate,
            FxPair.sell_fee_rate,
            FxPair.pool_version,
        )
        .join(FxPair, FxPair.id == FxShortPosition.pair_id)
        .where(FxShortPosition.user_id.in_(ids))
        .order_by(FxShortPosition.user_id, FxShortPosition.pair_id)
    )).all()
    restricted_by_user: dict[int, Decimal] = {}
    shorts_by_user: dict[int, list[tuple[FxShortPairSnapshot, object]]] = {}
    for (uid, pair_id, principal, interest, accrued, restricted,
         status, reduce_only, gold_reserve, foreign_reserve,
         buy_fee_rate, sell_fee_rate, pool_version) in short_rows:
        uid = int(uid)
        restricted_by_user[uid] = restricted_by_user.get(uid, ZERO) + Decimal(restricted)
        shorts_by_user.setdefault(uid, []).append((
            FxShortPairSnapshot(
                pair_id=int(pair_id),
                status=status,
                reduce_only=bool(reduce_only),
                gold_reserve=Decimal(gold_reserve),
                foreign_reserve=Decimal(foreign_reserve),
                buy_fee_rate=Decimal(buy_fee_rate),
                sell_fee_rate=Decimal(sell_fee_rate),
                pool_version=int(pool_version),
            ),
            SimpleNamespace(
                principal_foreign=Decimal(principal),
                interest_foreign=Decimal(interest),
                interest_last_accrued_at=accrued,
            ),
        ))

    lmsr_fee_rate = ZERO
    if market_meta:
        lmsr_fee_rate = await site_config.get_decimal_or(
            session, SELL_FEE_RATE_KEY, ZERO,
        )

    thresholds = credit_flags.get_flags().thresholds

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
                role="asset_sale",
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
                role="asset_sale",
            ))

        # 到这里 groups 只含正资产组；旧口径的 A = Σ L_asset（不可执行资产为 0）。
        asset_value = sum((group.value for group in groups if group.value is not None), ZERO)

        restricted = restricted_by_user.get(uid, ZERO)
        cash_invariant_broken = restricted > cash
        available_cash: Decimal | None = (
            None if cash_invariant_broken else cash - restricted
        )
        unknown_reasons: list[str] = []
        short_cover = ZERO
        short_marginal = ZERO
        marginal_incomplete = False

        for snapshot, position in shorts_by_user.get(uid, []):
            try:
                foreign_debt = pending_short_debt(position, daily_rate, now)
            except (ValueError, ArithmeticError):
                unknown_reasons.append(REASON_INVALID_SHORT_DEBT)
                groups.append(GroupLiquidation(
                    key=GroupKey("fx", snapshot.pair_id),
                    value=None,
                    executable=False,
                    blocked_reason=REASON_INVALID_SHORT_DEBT,
                    role="short_cover",
                ))
                continue
            if foreign_debt <= ZERO:
                continue
            quote = quote_fx_short_group(snapshot, foreign_debt=foreign_debt)
            groups.append(GroupLiquidation(
                key=GroupKey("fx", snapshot.pair_id),
                value=quote.gold_in,
                executable=quote.executable,
                blocked_reason=quote.blocked_reason,
                role="short_cover",
            ))
            if quote.gold_in is None:
                # 未知负债：绝不折算成 0/Infinity/NaN（spec §5.2）。
                unknown_reasons.append(quote.blocked_reason or BLOCKED_SHORT_QUOTE_FAILED)
            else:
                short_cover += quote.gold_in
            if quote.marginal_gold is None:
                marginal_incomplete = True
            else:
                short_marginal += quote.marginal_gold

        short_cover_cost: Decimal | None = None if unknown_reasons else short_cover
        short_marginal_debt: Decimal | None = (
            None if marginal_incomplete else short_marginal
        )
        blocked_reasons = list(unknown_reasons)
        if cash_invariant_broken:
            blocked_reasons.append(REASON_RESTRICTED_EXCEEDS_CASH)
        risk_status = RISK_STATUS_BLOCKED if blocked_reasons else RISK_STATUS_OK
        equity_unknown = short_cover_cost is None or cash_invariant_broken

        if equity_unknown:
            liquidation_equity: Decimal | None = None
        else:
            liquidation_equity = (
                cash + asset_value - debt_effective - short_cover_cost
            ).quantize(Q6)

        if equity_unknown or thresholds is None:
            risk_basis: Decimal | None = None
        else:
            # 展示金额保守向上量化，不拿截断值放宽准入（spec §6.1）。
            risk_basis = thresholds.risk_basis(
                debt=debt_effective,
                positive_assets=asset_value,
                short_cover=short_cover_cost,
            ).quantize(Q6, rounding=ROUND_CEILING)

        display_equity = cash + mtm_lmsr + mtm_fx - debt_effective
        if short_marginal_debt is not None:
            display_equity -= short_marginal_debt

        result[uid] = AccountValuation(
            user_id=uid,
            cash=cash,
            debt_persisted=debt,
            debt_effective=debt_effective,
            mtm_lmsr=mtm_lmsr,
            mtm_fx=mtm_fx,
            display_equity=display_equity.quantize(Q6),
            liquidation_equity=liquidation_equity,
            groups=tuple(sort_groups_by_liquidation(groups)),
            economic_version=int(user.economic_version or 0),
            short_cover_cost=short_cover_cost,
            short_marginal_debt=short_marginal_debt,
            risk_basis=risk_basis,
            available_cash=available_cash,
            restricted_cash=restricted,
            risk_status=risk_status,
            blocked_reason=(
                ",".join(sorted(set(blocked_reasons))) if blocked_reasons else None
            ),
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
