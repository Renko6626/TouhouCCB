"""WP2：FX 组清算报价纯函数（AMM 同值、F9 状态、零产出阻塞、无全量回退）。"""
import sys
import os
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.credit.fx_quote import FxPairSnapshot, quote_fx_group
from app.services.fx.amm import quote_sell

ZERO = Decimal("0")
ONE = Decimal("1")
MICRO = Decimal("0.000001")


def _pair(*, pair_id=1, status="trading", reduce_only=False, gold="100",
          foreign="100", fee="0"):
    return FxPairSnapshot(
        pair_id=pair_id, status=status, reduce_only=reduce_only,
        gold_reserve=Decimal(gold), foreign_reserve=Decimal(foreign),
        sell_fee_rate=Decimal(fee),
    )


def test_full_quote_matches_amm_quote_sell_field_by_field():
    pair = _pair(gold="120", foreign="80", fee="0.01")
    amount = Decimal("7.5")
    quote = quote_fx_group(
        pair, foreign_amount=amount, mode="full", partial_pct=ONE,
    )
    expected = quote_sell(amount, pair.gold_reserve, pair.foreign_reserve,
                          pair.sell_fee_rate)
    assert quote.blocked_reason is None
    assert (quote.foreign_in, quote.fee_foreign, quote.gold_out,
            quote.post_gold_reserve, quote.post_foreign_reserve) == (
        expected.input_amount, expected.fee_amount, expected.output_amount,
        expected.post_gold_reserve, expected.post_foreign_reserve,
    )
    assert quote.fee_foreign > ZERO
    assert quote.gold_out < quote_sell(
        amount, pair.gold_reserve, pair.foreign_reserve, ZERO,
    ).output_amount


@pytest.mark.parametrize("status,reduce_only,executable", [
    ("trading", False, True),
    ("trading", True, True),
    ("paused", True, True),
    ("paused", False, False),
    ("draft", True, False),
    ("closed", True, False),
    ("weird", True, False),
])
def test_f9_status_matrix(status, reduce_only, executable):
    quote = quote_fx_group(
        _pair(status=status, reduce_only=reduce_only),
        foreign_amount=Decimal("10"), mode="full", partial_pct=ONE,
    )
    if executable:
        assert quote.blocked_reason is None
        assert quote.gold_out > ZERO
        assert quote.post_gold_reserve < Decimal("100")
        assert quote.post_foreign_reserve > Decimal("100")
    else:
        assert quote.blocked_reason is not None
        assert quote.gold_out == ZERO
        assert quote.foreign_in == ZERO and quote.fee_foreign == ZERO
        assert quote.post_gold_reserve == Decimal("100")
        assert quote.post_foreign_reserve == Decimal("100")


def test_blocked_reason_names_distinguish_paused_from_closed_and_draft():
    assert quote_fx_group(
        _pair(status="paused", reduce_only=False), foreign_amount=Decimal("10"),
        mode="full", partial_pct=ONE,
    ).blocked_reason == "pair_paused"
    assert quote_fx_group(
        _pair(status="closed"), foreign_amount=Decimal("10"),
        mode="full", partial_pct=ONE,
    ).blocked_reason == "pair_closed"
    assert quote_fx_group(
        _pair(status="draft"), foreign_amount=Decimal("10"),
        mode="full", partial_pct=ONE,
    ).blocked_reason == "pair_draft"


def test_partial_ceils_to_micro_unit_and_caps_balance():
    quote = quote_fx_group(
        _pair(), foreign_amount=Decimal("100"), mode="partial",
        partial_pct=Decimal("0.10"),
    )
    assert quote.foreign_in == Decimal("10.000000")

    quote = quote_fx_group(
        _pair(gold="1000", foreign="100"), foreign_amount=Decimal("0.000001"),
        mode="partial", partial_pct=Decimal("0.10"),
    )
    assert quote.foreign_in == MICRO
    assert quote.blocked_reason is None

    # 10.0000005 × 10% = 1.00000005 → 向上量化到 1.000001
    quote = quote_fx_group(
        _pair(), foreign_amount=Decimal("10.0000005"), mode="partial",
        partial_pct=Decimal("0.10"),
    )
    assert quote.foreign_in == Decimal("1.000001")

    quote = quote_fx_group(
        _pair(), foreign_amount=Decimal("10.5"), mode="partial",
        partial_pct=Decimal("0.10"), unit=Decimal("0.5"),
    )
    assert quote.foreign_in == Decimal("1.5")


def test_partial_zero_output_blocks_without_full_fallback():
    """固定批次产出量化到 0 → 阻塞；即使全量可成交也不回退（controller 裁定）。"""
    pair = _pair(gold="0.1", foreign="1000000", fee="0")
    partial = quote_fx_group(
        pair, foreign_amount=Decimal("100"), mode="partial",
        partial_pct=Decimal("0.10"),
    )
    assert partial.blocked_reason == "quote_failed"
    assert partial.gold_out == ZERO and partial.foreign_in == ZERO
    assert partial.post_gold_reserve == pair.gold_reserve
    assert partial.post_foreign_reserve == pair.foreign_reserve

    full = quote_fx_group(
        pair, foreign_amount=Decimal("100"), mode="full", partial_pct=ONE,
    )
    assert full.blocked_reason is None
    assert full.gold_out == Decimal("0.000009")


def test_quote_failure_on_invalid_reserves_or_fee_is_blocked():
    for pair in (
        _pair(gold="0", foreign="100"),
        _pair(gold="100", foreign="0"),
        _pair(fee="1"),
        _pair(fee="-0.1"),
    ):
        quote = quote_fx_group(
            pair, foreign_amount=Decimal("10"), mode="full", partial_pct=ONE,
        )
        assert quote.blocked_reason == "quote_failed", pair
        assert quote.gold_out == ZERO


def test_zero_amount_is_nothing_to_sell_and_negative_raises():
    quote = quote_fx_group(
        _pair(), foreign_amount=ZERO, mode="full", partial_pct=ONE,
    )
    assert quote.blocked_reason == "nothing_to_sell" and quote.gold_out == ZERO
    with pytest.raises(ValueError):
        quote_fx_group(
            _pair(), foreign_amount=Decimal("-1"), mode="full", partial_pct=ONE,
        )
    with pytest.raises(ValueError):
        quote_fx_group(
            _pair(), foreign_amount=Decimal("NaN"), mode="full", partial_pct=ONE,
        )


def test_invalid_mode_and_partial_pct_raise():
    for kwargs in (
        dict(mode="half", partial_pct=ONE),
        dict(mode="partial", partial_pct=ZERO),
        dict(mode="partial", partial_pct=Decimal("1.2")),
        dict(mode="full", partial_pct=ONE, unit=ZERO),
    ):
        with pytest.raises(ValueError):
            quote_fx_group(_pair(), foreign_amount=Decimal("10"), **kwargs)
