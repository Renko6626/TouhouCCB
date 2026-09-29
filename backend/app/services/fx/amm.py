"""Constant-product FX AMM calculations.

The pool stores gold (G) and foreign currency (F).  Prices are expressed as
gold per foreign unit (G / F), while quote effective prices are output per
input unit.  Fees are charged in the input currency before the invariant
calculation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from .quantize import amount_down, amount_up

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class FxReserves:
    gold_reserve: Decimal
    foreign_reserve: Decimal


@dataclass(frozen=True)
class FxQuoteMath:
    input_amount: Decimal
    fee_amount: Decimal
    net_input: Decimal
    output_amount: Decimal
    post_gold_reserve: Decimal
    post_foreign_reserve: Decimal
    post_price: Decimal
    effective_price: Decimal


def _validate_amount(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if value <= ZERO:
        raise ValueError(f"{name} must be positive")


def _validate_reserves(gold_reserve: Decimal, foreign_reserve: Decimal) -> None:
    _validate_amount(gold_reserve, "gold reserve")
    _validate_amount(foreign_reserve, "foreign reserve")


def _fee(input_amount: Decimal, fee_rate: Decimal) -> Decimal:
    if not isinstance(fee_rate, Decimal):
        raise TypeError("fee rate must be Decimal")
    if not fee_rate.is_finite():
        raise ValueError("fee rate must be finite")
    if fee_rate < ZERO or fee_rate >= ONE:
        raise ValueError("fee rate must be >= 0 and < 1")
    fee = amount_up(input_amount * fee_rate)
    if fee >= input_amount:
        raise ValueError("fee leaves no input")
    return fee


def marginal_price(gold_reserve: Decimal, foreign_reserve: Decimal) -> Decimal:
    _validate_reserves(gold_reserve, foreign_reserve)
    return gold_reserve / foreign_reserve


def quote_buy(
    gold_in: Decimal,
    gold_reserve: Decimal,
    foreign_reserve: Decimal,
    fee_rate: Decimal,
) -> FxQuoteMath:
    """Quote gold input for foreign output."""
    _validate_amount(gold_in, "input")
    _validate_reserves(gold_reserve, foreign_reserve)
    fee = _fee(gold_in, fee_rate)
    net = gold_in - fee
    with localcontext() as ctx:
        ctx.prec = max(50, len(gold_in.as_tuple().digits) + len(gold_reserve.as_tuple().digits) + 20)
        post_gold = gold_reserve + net
        raw_output = foreign_reserve - (gold_reserve * foreign_reserve / post_gold)
    output = amount_down(raw_output)
    if output <= ZERO:
        raise ValueError("output rounds to zero")
    post_foreign = foreign_reserve - output
    if post_gold <= ZERO or post_foreign <= ZERO:
        raise ValueError("post reserve must be positive")
    return FxQuoteMath(
        gold_in,
        fee,
        net,
        output,
        post_gold,
        post_foreign,
        marginal_price(post_gold, post_foreign),
        output / gold_in,
    )


def quote_sell(
    foreign_in: Decimal,
    gold_reserve: Decimal,
    foreign_reserve: Decimal,
    fee_rate: Decimal,
) -> FxQuoteMath:
    """Quote foreign input for gold output."""
    _validate_amount(foreign_in, "input")
    _validate_reserves(gold_reserve, foreign_reserve)
    fee = _fee(foreign_in, fee_rate)
    net = foreign_in - fee
    with localcontext() as ctx:
        ctx.prec = max(50, len(foreign_in.as_tuple().digits) + len(foreign_reserve.as_tuple().digits) + 20)
        post_foreign = foreign_reserve + net
        raw_output = gold_reserve - (gold_reserve * foreign_reserve / post_foreign)
    output = amount_down(raw_output)
    if output <= ZERO:
        raise ValueError("output rounds to zero")
    post_gold = gold_reserve - output
    if post_gold <= ZERO or post_foreign <= ZERO:
        raise ValueError("post reserve must be positive")
    return FxQuoteMath(
        foreign_in,
        fee,
        net,
        output,
        post_gold,
        post_foreign,
        marginal_price(post_gold, post_foreign),
        output / foreign_in,
    )


def apply_quote(reserves: FxReserves, quote: FxQuoteMath) -> FxReserves:
    _validate_reserves(reserves.gold_reserve, reserves.foreign_reserve)
    _validate_amount(quote.post_gold_reserve, "post gold reserve")
    _validate_amount(quote.post_foreign_reserve, "post foreign reserve")
    return FxReserves(quote.post_gold_reserve, quote.post_foreign_reserve)
