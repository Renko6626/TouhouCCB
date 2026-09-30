"""WP2：FX 组清算报价纯函数（AMM 同值、F9 状态、零产出阻塞、无全量回退）。"""
import sys
import os
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.credit.fx_quote import (
    FxPairSnapshot,
    FxShortPairSnapshot,
    quote_fx_group,
    quote_fx_short_group,
)
from app.services.fx.amm import quote_buy_exact_out, quote_sell

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


def _short_pair(*, status="trading", reduce_only=False, gold="100",
                foreign="100", buy_fee="0"):
    return FxShortPairSnapshot(
        pair_id=1, status=status, reduce_only=reduce_only,
        gold_reserve=Decimal(gold), foreign_reserve=Decimal(foreign),
        buy_fee_rate=Decimal(buy_fee),
    )


def test_short_quote_is_exact_output_whole_debt_cost():
    """K 来自 quote_buy_exact_out（含买入费用），不是 q×边际价或 exact-input。"""
    pair = _short_pair(gold="120", foreign="80", buy_fee="0.01")
    debt = Decimal("7.5")
    quote = quote_fx_short_group(pair, foreign_debt=debt)
    expected = quote_buy_exact_out(debt, Decimal("120"), Decimal("80"), Decimal("0.01"))
    assert quote.blocked_reason is None and quote.executable is True
    assert quote.gold_in == expected.input_amount
    assert quote.fee_gold == expected.fee_amount
    assert quote.gold_in > debt * (Decimal("120") / Decimal("80"))  # slippage + fee
    # marginal estimate is sourced but is not the executable cover cost
    assert quote.marginal_gold == (debt * Decimal("120") / Decimal("80")).quantize(
        Decimal("0.000001"),
    )
    assert quote.marginal_gold != quote.gold_in


def test_short_quote_keeps_finite_k_for_paused_but_marks_nonexecutable():
    paused = quote_fx_short_group(
        _short_pair(status="paused", reduce_only=False), foreign_debt=Decimal("10"),
    )
    expected = quote_buy_exact_out(
        Decimal("10"), Decimal("100"), Decimal("100"), ZERO,
    ).input_amount
    assert paused.gold_in == expected       # math is finite...
    assert paused.executable is False       # ...but the market is fully paused
    assert paused.blocked_reason == "pair_paused"

    reduce_only = quote_fx_short_group(
        _short_pair(status="paused", reduce_only=True), foreign_debt=Decimal("10"),
    )
    assert reduce_only.gold_in == expected and reduce_only.executable is True


def test_short_quote_q_ge_foreign_reserve_and_invalid_inputs_are_unknown():
    trading = _short_pair(gold="100", foreign="100")
    unquotable = quote_fx_short_group(trading, foreign_debt=Decimal("100"))
    assert unquotable.gold_in is None and unquotable.fee_gold is None
    assert unquotable.executable is False
    assert unquotable.blocked_reason == "insufficient_pool_foreign"
    # a sourced marginal estimate may still exist, but K must stay None
    assert unquotable.marginal_gold == Decimal("100.000000")

    for gold, foreign in (("0", "100"), ("100", "0")):
        invalid = quote_fx_short_group(
            _short_pair(gold=gold, foreign=foreign), foreign_debt=Decimal("5"),
        )
        assert invalid.gold_in is None
        assert invalid.executable is False
        assert invalid.blocked_reason == "invalid_short_reserve"


def test_short_quote_negative_or_nonfinite_debt_raises_or_blocks():
    with pytest.raises(ValueError):
        quote_fx_short_group(_short_pair(), foreign_debt=Decimal("-1"))
    with pytest.raises(ValueError):
        quote_fx_short_group(_short_pair(), foreign_debt=Decimal("NaN"))
