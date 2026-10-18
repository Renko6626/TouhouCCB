"""Decimal quantization rules for FX amounts."""

from decimal import Decimal, ROUND_DOWN, ROUND_UP

AMOUNT_QUANT = Decimal("0.000001")


def amount_down(value: Decimal) -> Decimal:
    return value.quantize(AMOUNT_QUANT, rounding=ROUND_DOWN)


def amount_up(value: Decimal) -> Decimal:
    return value.quantize(AMOUNT_QUANT, rounding=ROUND_UP)
