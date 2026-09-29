"""LMSR 组清算报价（纯数学：不查库、不写库、不推进任何状态）。

同一 market 的全部 outcome 必须放在**同一个滚动 q 副本**上按 ``outcome_id``
升序模拟卖出：先卖出的腿改变后续腿的成交价，逐持仓在未变化 q 上独立求和会
高估回收（见 ``wealth.compute_users_holdings_value`` 的旧口径）。

``gross`` 为 cost 差（cost_before − cost_after），``fee = gross × fee_rate``，
``net = gross − fee``；``gross < 0`` 的腿按负收益跳过（不卖、不删持仓、不进
q 副本），组内其余腿继续。市场不可交易时整组阻塞、``net = 0``。

``OutcomeSnapshot.status`` / ``closes_at`` 是**市场级**字段（``outcome`` 表没有
status），调用方从 ``Market`` 复制到该 market 的每个 outcome 快照上。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Literal, Mapping, Sequence

from app.services.lmsr import calculate_lmsr_cost, quantize_cost
from app.services.market_open import market_is_open

ZERO = Decimal("0")
ONE = Decimal("1")
Q6 = Decimal("0.000001")

Mode = Literal["partial", "full"]

#: 组级阻塞原因（WP7 写入 ``last_blocked_reason``；任何非 None 都是"不可执行"）。
BLOCKED_MARKET_NOT_OPEN = "market_not_open"
BLOCKED_NEGATIVE_PROCEEDS = "negative_proceeds"
BLOCKED_NO_OUTCOMES = "no_outcomes"
BLOCKED_UNKNOWN_OUTCOME = "unknown_outcome"


@dataclass(frozen=True)
class OutcomeSnapshot:
    outcome_id: int
    total_shares: Decimal
    status: str
    closes_at: datetime | None


@dataclass(frozen=True)
class LmsrLeg:
    outcome_id: int
    amount: Decimal
    gross: Decimal
    fee: Decimal
    net: Decimal


@dataclass(frozen=True)
class LmsrGroupQuote:
    market_id: int
    mode: Mode
    legs: tuple[LmsrLeg, ...]
    gross: Decimal
    fee: Decimal
    net: Decimal
    blocked_reason: str | None


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


def _ceil_to_unit(value: Decimal, unit: Decimal) -> Decimal:
    """向上取到 ``unit`` 的整数倍（F12：LMSR 最小卖出单位 = 1 股）。"""
    if unit <= ZERO:
        raise ValueError(f"unit must be positive: {unit!r}")
    steps = (value / unit).to_integral_value(rounding=ROUND_CEILING)
    return steps * unit


def _partial_amount(amount: Decimal, partial_pct: Decimal, unit: Decimal) -> Decimal:
    """``min(amount, ceil_to_unit(amount × partial_pct, unit))``，再封顶持仓。"""
    target = _ceil_to_unit(amount * partial_pct, unit).quantize(
        Q6, rounding=ROUND_CEILING,
    )
    return amount if target > amount else target


def _blocked(market_id: int, mode: Mode, reason: str | None) -> LmsrGroupQuote:
    return LmsrGroupQuote(
        market_id=market_id, mode=mode, legs=(), gross=ZERO, fee=ZERO, net=ZERO,
        blocked_reason=reason,
    )


def quote_lmsr_group(
    outcomes: Sequence[OutcomeSnapshot],
    positions: Mapping[int, Decimal],
    *,
    market_id: int,
    b: float,
    fee_rate: Decimal,
    mode: Mode,
    partial_pct: Decimal,
    unit: Decimal = Decimal("1"),
) -> LmsrGroupQuote:
    """按滚动 q 生成整组卖出报价；纯函数，调用方负责落库与镜像更新。"""
    if mode not in ("partial", "full"):
        raise ValueError(f"unknown mode: {mode!r}")
    liquidity = _as_decimal(b, "b")
    if liquidity <= ZERO:
        raise ValueError(f"b must be positive: {b!r}")
    fee = _as_decimal(fee_rate, "fee_rate")
    if fee < ZERO or fee >= ONE:
        raise ValueError(f"fee_rate must be in [0, 1): {fee!r}")
    pct = _as_decimal(partial_pct, "partial_pct")
    if mode == "partial" and not (ZERO < pct <= ONE):
        raise ValueError(f"partial_pct must be in (0, 1]: {pct!r}")
    step = _as_decimal(unit, "unit")
    if step <= ZERO:
        raise ValueError(f"unit must be positive: {unit!r}")

    ordered = sorted(outcomes, key=lambda item: int(item.outcome_id))
    seen: set[int] = set()
    for item in ordered:
        oid = int(item.outcome_id)
        if oid in seen:
            raise ValueError(f"duplicate outcome_id: {oid}")
        seen.add(oid)

    held: dict[int, Decimal] = {}
    for oid, amount in positions.items():
        value = _as_decimal(amount, "position amount")
        if value > ZERO:
            held[int(oid)] = value

    if not ordered:
        return _blocked(market_id, mode, BLOCKED_NO_OUTCOMES if held else None)
    # status/closes_at 是市场级字段，同一 market 的每个快照都相同。
    first = ordered[0]
    if not market_is_open(first.status, first.closes_at):
        return _blocked(market_id, mode, BLOCKED_MARKET_NOT_OPEN)
    if not held:
        return _blocked(market_id, mode, None)

    q = [float(item.total_shares) for item in ordered]
    index_of = {int(item.outcome_id): i for i, item in enumerate(ordered)}
    b_float = float(liquidity)
    cost_before = calculate_lmsr_cost(q, b_float)

    legs: list[LmsrLeg] = []
    skipped_negative = False
    for item in ordered:
        oid = int(item.outcome_id)
        amount = held.get(oid)
        if amount is None:
            continue
        if mode == "partial":
            amount = _partial_amount(amount, pct, step)
        if amount <= ZERO:
            continue
        idx = index_of[oid]
        after = list(q)
        after[idx] -= float(amount)
        cost_after = calculate_lmsr_cost(after, b_float)
        gross = quantize_cost(cost_before - cost_after)
        if gross < ZERO:
            # 负收益腿：不卖、不改 q 副本、不删持仓；继续处理后续腿。
            skipped_negative = True
            continue
        leg_fee = quantize_cost(gross * fee)
        legs.append(LmsrLeg(
            outcome_id=oid, amount=amount, gross=gross, fee=leg_fee, net=gross - leg_fee,
        ))
        q = after
        cost_before = cost_after

    unknown = any(oid not in index_of for oid in held)
    if not legs:
        if skipped_negative:
            return _blocked(market_id, mode, BLOCKED_NEGATIVE_PROCEEDS)
        if unknown:
            return _blocked(market_id, mode, BLOCKED_UNKNOWN_OUTCOME)
        return _blocked(market_id, mode, None)

    total_gross = sum((leg.gross for leg in legs), ZERO)
    total_fee = sum((leg.fee for leg in legs), ZERO)
    return LmsrGroupQuote(
        market_id=market_id,
        mode=mode,
        legs=tuple(legs),
        gross=total_gross,
        fee=total_fee,
        net=total_gross - total_fee,
        blocked_reason=None,
    )
