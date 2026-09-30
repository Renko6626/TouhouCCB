from decimal import Decimal

import pytest

from app.services.fx.amm import (
    FxQuoteMath,
    FxReserves,
    apply_quote,
    marginal_price,
    quote_buy,
    quote_sell,
)


D = Decimal


def test_buy_quote_moves_reserves_and_has_constant_product_before_output_floor():
    quote = quote_buy(D("10"), D("100"), D("200"), D("0"))

    assert isinstance(quote, FxQuoteMath)
    assert quote.input_amount == D("10")
    assert quote.fee_amount == D("0.000000")
    assert quote.net_input == D("10.000000")
    assert quote.output_amount == D("18.181818")
    assert quote.post_gold_reserve == D("110.000000")
    assert quote.post_foreign_reserve == D("181.818182")
    assert quote.post_gold_reserve * quote.post_foreign_reserve >= D("20000")
    assert quote.post_price == marginal_price(quote.post_gold_reserve, quote.post_foreign_reserve)
    assert quote.effective_price == quote.output_amount / quote.input_amount


def test_sell_quote_moves_reserves_and_apply_quote_matches_it():
    quote = quote_sell(D("20"), D("100"), D("200"), D("0"))

    assert quote.input_amount == D("20")
    assert quote.output_amount == D("9.090909")
    assert quote.post_gold_reserve == D("90.909091")
    assert quote.post_foreign_reserve == D("220.000000")
    assert apply_quote(FxReserves(D("100"), D("200")), quote) == FxReserves(
        D("90.909091"), D("220.000000")
    )


def test_fee_is_rounded_up_before_net_input_and_output_is_rounded_down():
    quote = quote_buy(D("1.000000"), D("100"), D("100"), D("0.0000001"))

    assert quote.fee_amount == D("0.000001")
    assert quote.net_input == D("0.999999")
    assert quote.output_amount == D("0.990098")


def test_rejects_tiny_trade_that_rounds_to_zero_output():
    with pytest.raises(ValueError, match="output"):
        quote_buy(D("0.000001"), D("100"), D("100"), D("0"))


@pytest.mark.parametrize(
    "fn,args",
    [
        (quote_buy, (D("1"), D("0"), D("1"), D("0"))),
        (quote_buy, (D("1"), D("1"), D("-1"), D("0"))),
        (quote_sell, (D("1"), D("0"), D("1"), D("0"))),
        (quote_sell, (D("1"), D("1"), D("-1"), D("0"))),
    ],
)
def test_rejects_non_positive_reserves(fn, args):
    with pytest.raises(ValueError, match="reserve"):
        fn(*args)


def test_rejects_non_positive_input_and_full_fee():
    with pytest.raises(ValueError, match="input"):
        quote_buy(D("0"), D("100"), D("100"), D("0"))
    with pytest.raises(ValueError, match="fee"):
        quote_sell(D("1"), D("100"), D("100"), D("1"))


def test_rejects_invalid_fee_rate_and_invalid_applied_post_reserves():
    with pytest.raises(ValueError, match="fee"):
        quote_buy(D("1"), D("100"), D("100"), D("-0.1"))
    quote = quote_buy(D("1"), D("100"), D("100"), D("0"))
    invalid_quote = FxQuoteMath(
        quote.input_amount,
        quote.fee_amount,
        quote.net_input,
        quote.output_amount,
        D("0"),
        quote.post_foreign_reserve,
        quote.post_price,
        quote.effective_price,
    )
    with pytest.raises(ValueError, match="reserve"):
        apply_quote(FxReserves(D("100"), D("100")), invalid_quote)


@pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), D("-Infinity")])
def test_rejects_non_finite_inputs(value):
    with pytest.raises(ValueError, match="finite"):
        quote_buy(value, D("100"), D("100"), D("0"))


@pytest.mark.parametrize("gold,foreign", [(D("NaN"), D("100")), (D("100"), D("Infinity"))])
def test_rejects_non_finite_reserves(gold, foreign):
    with pytest.raises(ValueError, match="finite"):
        quote_sell(D("1"), gold, foreign, D("0"))


@pytest.mark.parametrize("fee_rate", [D("NaN"), D("Infinity"), D("-Infinity")])
def test_rejects_non_finite_fee_rates(fee_rate):
    with pytest.raises(ValueError, match="finite"):
        quote_buy(D("1"), D("100"), D("100"), fee_rate)


@pytest.mark.parametrize("post_gold,post_foreign", [(D("NaN"), D("100")), (D("100"), D("Infinity"))])
def test_apply_quote_rejects_non_finite_post_reserves(post_gold, post_foreign):
    quote = quote_buy(D("1"), D("100"), D("100"), D("0"))
    malformed = FxQuoteMath(
        quote.input_amount,
        quote.fee_amount,
        quote.net_input,
        quote.output_amount,
        post_gold,
        post_foreign,
        quote.post_price,
        quote.effective_price,
    )
    with pytest.raises(ValueError, match="finite"):
        apply_quote(FxReserves(D("100"), D("100")), malformed)


@pytest.mark.parametrize('q,fee', [('0.000001','0.03'), ('199.990000','0.001'), ('17.123456','0.33333333')])
def test_exact_output_buys_requested_amount_and_preserves_stock(q, fee):
    from app.services.fx.amm import quote_buy_exact_out
    result = quote_buy_exact_out(D(q), D('100'), D('200'), D(fee))
    assert result.output_amount == D(q)
    assert result.post_foreign_reserve + result.output_amount == D('200')
    assert result.post_gold_reserve * result.post_foreign_reserve >= D('20000')
    assert quote_buy(result.input_amount, D('100'), D('200'), D(fee)).output_amount >= D(q)


@pytest.mark.parametrize('q,g,f,fee', [('200','100','200','0'), ('0.0000001','100','200','0'), ('199.999999','9999999999','200','0'), ('1','100','200','1')])
def test_exact_output_rejects_unexecutable_or_unstorable_quote(q,g,f,fee):
    from app.services.fx.amm import quote_buy_exact_out
    with pytest.raises(ValueError):
        quote_buy_exact_out(D(q), D(g), D(f), D(fee))
