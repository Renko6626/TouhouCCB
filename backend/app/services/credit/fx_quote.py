"""FX pair 组清算报价（纯数学：复用 ``fx.amm.quote_sell``，不查库）。

可执行性按 F9 双轴判定：

- ``trading``（无论 ``reduce_only``）→ 可执行；
- ``paused + reduce_only=true`` → 可执行（管理端显式选择的只减仓）；
- ``paused + reduce_only=false``、``draft``、``closed``、未知状态 → 阻塞、产出 0。

``foreign_in`` 为卖出外币数量（``partial`` 时按固定比例向上量化并封顶钱包余额），
``fee_foreign`` 已在 AMM 内从输入中扣除，``gold_out`` 即净回收。固定批次报价失败
（产出量化到 0 / 储备非法）时**不回退全量**，直接返回 ``quote_failed`` 阻塞。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Literal

from app.services.fx.amm import marginal_price, quote_buy_exact_out, quote_sell

ZERO = Decimal("0")
ONE = Decimal("1")
Q6 = Decimal("0.000001")

Mode = Literal["partial", "full"]

#: 组级阻塞原因（WP7 写入 ``last_blocked_reason``）。
BLOCKED_PAIR_PAUSED = "pair_paused"
BLOCKED_PAIR_DRAFT = "pair_draft"
BLOCKED_PAIR_CLOSED = "pair_closed"
BLOCKED_PAIR_STATUS_UNKNOWN = "pair_status_unknown"
BLOCKED_QUOTE_FAILED = "quote_failed"
BLOCKED_NOTHING_TO_SELL = "nothing_to_sell"

#: 空头回补（exact-output 买足欠币）阻塞原因（WP2b1 / spec §5.2、§8.2）。
#: ``insufficient_pool_foreign`` 是设计明确要求的名字：欠币 q >= 池外币 F。
BLOCKED_SHORT_INSUFFICIENT_POOL = "insufficient_pool_foreign"
BLOCKED_SHORT_INVALID_RESERVE = "invalid_short_reserve"
BLOCKED_SHORT_QUOTE_FAILED = "short_quote_failed"


@dataclass(frozen=True)
class FxPairSnapshot:
    pair_id: int
    status: str            # draft|trading|paused|closed
    reduce_only: bool
    gold_reserve: Decimal
    foreign_reserve: Decimal
    sell_fee_rate: Decimal


@dataclass(frozen=True)
class FxGroupQuote:
    pair_id: int
    mode: Mode
    foreign_in: Decimal
    fee_foreign: Decimal
    gold_out: Decimal
    post_gold_reserve: Decimal
    post_foreign_reserve: Decimal
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
    """向上取到 ``unit`` 的整数倍（F12：FX 最小卖出单位 = 0.000001 外币）。"""
    if unit <= ZERO:
        raise ValueError(f"unit must be positive: {unit!r}")
    steps = (value / unit).to_integral_value(rounding=ROUND_CEILING)
    return steps * unit


def _partial_amount(amount: Decimal, partial_pct: Decimal, unit: Decimal) -> Decimal:
    target = _ceil_to_unit(amount * partial_pct, unit).quantize(
        Q6, rounding=ROUND_CEILING,
    )
    return amount if target > amount else target


def _status_blocked_reason(status: str, reduce_only: bool) -> str | None:
    normalized = (status or "").strip().lower()
    if normalized == "trading":
        return None
    if normalized == "paused":
        return None if reduce_only else BLOCKED_PAIR_PAUSED
    if normalized == "draft":
        return BLOCKED_PAIR_DRAFT
    if normalized == "closed":
        return BLOCKED_PAIR_CLOSED
    return BLOCKED_PAIR_STATUS_UNKNOWN


def _blocked(
    pair: FxPairSnapshot, mode: Mode, reason: str,
    gold_reserve: Decimal, foreign_reserve: Decimal,
) -> FxGroupQuote:
    return FxGroupQuote(
        pair_id=pair.pair_id,
        mode=mode,
        foreign_in=ZERO,
        fee_foreign=ZERO,
        gold_out=ZERO,
        post_gold_reserve=gold_reserve,
        post_foreign_reserve=foreign_reserve,
        blocked_reason=reason,
    )


def quote_fx_group(
    pair: FxPairSnapshot,
    *,
    foreign_amount: Decimal,
    mode: Mode,
    partial_pct: Decimal,
    unit: Decimal = Decimal("0.000001"),
) -> FxGroupQuote:
    """按当前储备报价卖出钱包内全部（或固定比例）外币；纯函数。"""
    if mode not in ("partial", "full"):
        raise ValueError(f"unknown mode: {mode!r}")
    amount = _as_decimal(foreign_amount, "foreign_amount")
    if amount < ZERO:
        raise ValueError(f"foreign_amount must be >= 0: {foreign_amount!r}")
    pct = _as_decimal(partial_pct, "partial_pct")
    if mode == "partial" and not (ZERO < pct <= ONE):
        raise ValueError(f"partial_pct must be in (0, 1]: {pct!r}")
    step = _as_decimal(unit, "unit")
    if step <= ZERO:
        raise ValueError(f"unit must be positive: {unit!r}")

    gold_reserve = _as_decimal(pair.gold_reserve, "gold_reserve")
    foreign_reserve = _as_decimal(pair.foreign_reserve, "foreign_reserve")

    if amount <= ZERO:
        return _blocked(
            pair, mode, BLOCKED_NOTHING_TO_SELL, gold_reserve, foreign_reserve,
        )

    foreign_in = amount if mode == "full" else _partial_amount(amount, pct, step)
    if foreign_in <= ZERO:
        return _blocked(
            pair, mode, BLOCKED_NOTHING_TO_SELL, gold_reserve, foreign_reserve,
        )

    reason = _status_blocked_reason(pair.status, bool(pair.reduce_only))
    if reason is not None:
        return _blocked(pair, mode, reason, gold_reserve, foreign_reserve)

    sell_fee_rate = _as_decimal(pair.sell_fee_rate, "sell_fee_rate")
    try:
        quoted = quote_sell(foreign_in, gold_reserve, foreign_reserve, sell_fee_rate)
    except (ValueError, ArithmeticError):
        # 固定批次不可执行：不回退全量，也不改动储备。
        return _blocked(
            pair, mode, BLOCKED_QUOTE_FAILED, gold_reserve, foreign_reserve,
        )

    return FxGroupQuote(
        pair_id=pair.pair_id,
        mode=mode,
        foreign_in=quoted.input_amount,
        fee_foreign=quoted.fee_amount,
        gold_out=quoted.output_amount,
        post_gold_reserve=quoted.post_gold_reserve,
        post_foreign_reserve=quoted.post_foreign_reserve,
        blocked_reason=None,
    )


@dataclass(frozen=True)
class FxShortPairSnapshot:
    """空头回补所需的 pair 快照：储备 + 两侧费率 + 状态 + pool 版本。

    与正资产 ``FxPairSnapshot`` 分开：正资产只用 ``sell_fee_rate``，空头回补
    走买入方向，必须用 ``buy_fee_rate``；两者混用会把费用算错方向。``pool_version``
    供下游（强平/门闩）复检同一份储备版本。
    """

    pair_id: int
    status: str            # draft|trading|paused|closed
    reduce_only: bool
    gold_reserve: Decimal
    foreign_reserve: Decimal
    buy_fee_rate: Decimal
    sell_fee_rate: Decimal = ZERO
    pool_version: int = 0


@dataclass(frozen=True)
class FxShortQuote:
    """一个 pair 的整仓回补成本。

    ``gold_in`` / ``fee_gold`` 是**可完整报价**时的精确 K；``None`` 表示债务
    无法完整报价（q>=F、储备非法、溢出、非有限）。未知负债绝不写 0、Infinity
    或 NaN。``marginal_gold`` 是**有来源**的展示估计（储备合法即有），它不等于
    可执行回补价。``executable`` 与报价完整性分开：全停市场数学有限但不可执行。
    """

    pair_id: int
    foreign_debt: Decimal
    gold_in: Decimal | None
    fee_gold: Decimal | None
    marginal_gold: Decimal | None
    executable: bool
    blocked_reason: str | None


def _short_blocked(
    pair: FxShortPairSnapshot, debt: Decimal, reason: str,
    *, marginal: Decimal | None,
) -> FxShortQuote:
    return FxShortQuote(
        pair_id=pair.pair_id,
        foreign_debt=debt,
        gold_in=None,
        fee_gold=None,
        marginal_gold=marginal,
        executable=False,
        blocked_reason=reason,
    )


def quote_fx_short_group(
    pair: FxShortPairSnapshot,
    *,
    foreign_debt: Decimal,
) -> FxShortQuote:
    """按当前储备买足 ``foreign_debt`` 的整仓回补成本（纯函数，不查库）。

    使用 ``quote_buy_exact_out``（含买入费率、向上量化）而不是 ``q × 边际价``
    或普通 exact-input 报价。q >= F 或任何非有限/溢出情形返回 ``gold_in=None``，
    由调用方把 E/B 标为未知，绝不令 K=0。
    """
    debt = _as_decimal(foreign_debt, "foreign_debt")
    if debt < ZERO:
        raise ValueError(f"foreign_debt must be >= 0: {foreign_debt!r}")

    status_reason = _status_blocked_reason(pair.status, bool(pair.reduce_only))
    if debt == ZERO:
        return FxShortQuote(
            pair_id=pair.pair_id, foreign_debt=debt, gold_in=ZERO, fee_gold=ZERO,
            marginal_gold=ZERO, executable=status_reason is None,
            blocked_reason=status_reason,
        )

    try:
        gold_reserve = _as_decimal(pair.gold_reserve, "gold_reserve")
        foreign_reserve = _as_decimal(pair.foreign_reserve, "foreign_reserve")
    except ValueError:
        return _short_blocked(pair, debt, BLOCKED_SHORT_INVALID_RESERVE, marginal=None)
    if gold_reserve <= ZERO or foreign_reserve <= ZERO:
        return _short_blocked(pair, debt, BLOCKED_SHORT_INVALID_RESERVE, marginal=None)

    # 有来源的边际估计：储备合法即可给出，与是否可完整报价/可执行无关。
    try:
        marginal = (debt * marginal_price(gold_reserve, foreign_reserve)).quantize(Q6)
    except (ValueError, ArithmeticError):
        marginal = None

    if debt >= foreign_reserve:
        return _short_blocked(pair, debt, BLOCKED_SHORT_INSUFFICIENT_POOL, marginal=marginal)

    try:
        quoted = quote_buy_exact_out(
            debt, gold_reserve, foreign_reserve, _as_decimal(pair.buy_fee_rate, "buy_fee_rate"),
        )
    except (ValueError, ArithmeticError):
        return _short_blocked(pair, debt, BLOCKED_SHORT_QUOTE_FAILED, marginal=marginal)

    return FxShortQuote(
        pair_id=pair.pair_id,
        foreign_debt=debt,
        gold_in=quoted.input_amount,
        fee_gold=quoted.fee_amount,
        marginal_gold=marginal,
        executable=status_reason is None,
        blocked_reason=status_reason,
    )
