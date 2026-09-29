"""WP2：LMSR 组清算报价纯函数（滚动 q、ceil、费率锚、阻塞原因）。"""
import sys
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.credit import lmsr_quote
from app.services.credit.lmsr_quote import OutcomeSnapshot, quote_lmsr_group
from app.services.lmsr import calculate_lmsr_cost, quantize_cost

B = 100.0
FUTURE = datetime.now(timezone.utc) + timedelta(days=7)
PAST = datetime.now(timezone.utc) - timedelta(days=1)
ZERO = Decimal("0")


def _snap(oid: int, shares: str, status: str = "trading", closes_at=FUTURE):
    return OutcomeSnapshot(
        outcome_id=oid, total_shares=Decimal(shares), status=status, closes_at=closes_at,
    )


def _rolling_expected(outcomes, positions, fee_rate=ZERO):
    """测试内独立复算滚动 q 结果（不复用产品实现）。"""
    q = [float(item.total_shares) for item in outcomes]
    index = {item.outcome_id: i for i, item in enumerate(outcomes)}
    cost = calculate_lmsr_cost(q, B)
    legs = []
    for item in outcomes:
        amount = positions.get(item.outcome_id)
        if amount is None or amount <= 0:
            continue
        after = list(q)
        after[index[item.outcome_id]] -= float(amount)
        new_cost = calculate_lmsr_cost(after, B)
        gross = quantize_cost(cost - new_cost)
        fee = quantize_cost(gross * fee_rate)
        legs.append((item.outcome_id, amount, gross, fee, gross - fee))
        q, cost = after, new_cost
    return legs


def test_rolling_q_differs_from_independent_sum():
    outcomes = [_snap(1, "120"), _snap(2, "80"), _snap(3, "40")]
    positions = {1: Decimal("30"), 2: Decimal("20")}

    quote = quote_lmsr_group(
        outcomes, positions, market_id=7, b=B, fee_rate=ZERO,
        mode="full", partial_pct=Decimal("1"),
    )
    expected = _rolling_expected(outcomes, positions)
    assert [(leg.outcome_id, leg.amount, leg.gross, leg.fee, leg.net) for leg in quote.legs] \
        == expected
    assert quote.blocked_reason is None
    assert quote.gross == sum((leg[2] for leg in expected), ZERO)
    assert quote.net == quote.gross

    # 逐持仓在未变化 q 上独立全卖的旧口径
    q0 = [float(item.total_shares) for item in outcomes]
    cost0 = calculate_lmsr_cost(q0, B)
    independent = ZERO
    for oid, amount in positions.items():
        after = list(q0)
        after[oid - 1] -= float(amount)
        independent += quantize_cost(cost0 - calculate_lmsr_cost(after, B))
    assert independent != quote.net
    assert abs(independent - quote.net) > Decimal("0.000001")


def test_outcome_order_does_not_change_result():
    outcomes = [_snap(1, "120"), _snap(2, "80"), _snap(3, "40")]
    positions = {1: Decimal("30"), 2: Decimal("20")}
    forward = quote_lmsr_group(
        outcomes, positions, market_id=7, b=B, fee_rate=ZERO,
        mode="full", partial_pct=Decimal("1"),
    )
    backward = quote_lmsr_group(
        list(reversed(outcomes)), positions, market_id=7, b=B, fee_rate=ZERO,
        mode="full", partial_pct=Decimal("1"),
    )
    assert forward == backward
    assert [leg.outcome_id for leg in forward.legs] == [1, 2]


def test_partial_ceils_to_unit_and_caps_holding():
    outcomes = [_snap(1, "1000"), _snap(2, "1000")]
    quote = quote_lmsr_group(
        outcomes, {1: Decimal("100.5")}, market_id=1, b=B, fee_rate=ZERO,
        mode="partial", partial_pct=Decimal("0.10"),
    )
    assert quote.legs[0].amount == Decimal("11")

    quote = quote_lmsr_group(
        outcomes, {1: Decimal("15")}, market_id=1, b=B, fee_rate=ZERO,
        mode="partial", partial_pct=Decimal("0.10"),
    )
    assert quote.legs[0].amount == Decimal("2")

    # 小于一个单位时封顶持仓，不会放大
    quote = quote_lmsr_group(
        outcomes, {1: Decimal("0.4")}, market_id=1, b=B, fee_rate=ZERO,
        mode="partial", partial_pct=Decimal("0.10"),
    )
    assert quote.legs[0].amount == Decimal("0.4")

    # 自定义 unit：0.5 股粒度
    quote = quote_lmsr_group(
        outcomes, {1: Decimal("10.5")}, market_id=1, b=B, fee_rate=ZERO,
        mode="partial", partial_pct=Decimal("0.10"), unit=Decimal("0.5"),
    )
    assert quote.legs[0].amount == Decimal("1.5")


def test_full_mode_ignores_partial_pct():
    outcomes = [_snap(1, "1000")]
    quote = quote_lmsr_group(
        outcomes, {1: Decimal("12.5")}, market_id=1, b=B, fee_rate=ZERO,
        mode="full", partial_pct=Decimal("0.001"),
    )
    assert quote.legs[0].amount == Decimal("12.5")


def test_fee_rate_applies_per_leg_with_zero_anchor():
    outcomes = [_snap(1, "120"), _snap(2, "80")]
    positions = {1: Decimal("30"), 2: Decimal("20")}

    free = quote_lmsr_group(
        outcomes, positions, market_id=1, b=B, fee_rate=ZERO,
        mode="full", partial_pct=Decimal("1"),
    )
    assert all(leg.fee == ZERO for leg in free.legs)
    assert free.net == free.gross

    charged = quote_lmsr_group(
        outcomes, positions, market_id=1, b=B, fee_rate=Decimal("0.02"),
        mode="full", partial_pct=Decimal("1"),
    )
    assert all(leg.fee == quantize_cost(leg.gross * Decimal("0.02")) for leg in charged.legs)
    assert charged.fee == sum((leg.fee for leg in charged.legs), ZERO)
    assert charged.net == charged.gross - charged.fee
    assert charged.gross == free.gross


@pytest.mark.parametrize("status,closes_at", [
    ("halt", FUTURE),
    ("trading", PAST),
    ("settled", FUTURE),
])
def test_market_not_open_blocks_group_with_zero_net(status, closes_at):
    outcomes = [_snap(1, "120", status=status, closes_at=closes_at)]
    quote = quote_lmsr_group(
        outcomes, {1: Decimal("30")}, market_id=3, b=B, fee_rate=ZERO,
        mode="full", partial_pct=Decimal("1"),
    )
    assert quote.blocked_reason == "market_not_open"
    assert quote.net == ZERO and quote.gross == ZERO and quote.fee == ZERO
    assert quote.legs == ()


def test_negative_proceeds_leg_is_skipped_but_others_sell(monkeypatch):
    costs = iter([100.0, 101.0, 98.0])
    monkeypatch.setattr(lmsr_quote, "calculate_lmsr_cost", lambda shares, b: next(costs))
    outcomes = [_snap(1, "100"), _snap(2, "100")]
    quote = quote_lmsr_group(
        outcomes, {1: Decimal("10"), 2: Decimal("10")}, market_id=1, b=B,
        fee_rate=ZERO, mode="full", partial_pct=Decimal("1"),
    )
    assert [leg.outcome_id for leg in quote.legs] == [2]
    assert quote.legs[0].gross == Decimal("2")
    assert quote.net == Decimal("2")
    assert quote.blocked_reason is None


def test_all_negative_proceeds_blocks_group(monkeypatch):
    costs = iter([100.0, 101.0, 102.0])
    monkeypatch.setattr(lmsr_quote, "calculate_lmsr_cost", lambda shares, b: next(costs))
    outcomes = [_snap(1, "100"), _snap(2, "100")]
    quote = quote_lmsr_group(
        outcomes, {1: Decimal("10"), 2: Decimal("10")}, market_id=1, b=B,
        fee_rate=ZERO, mode="full", partial_pct=Decimal("1"),
    )
    assert quote.blocked_reason == "negative_proceeds"
    assert quote.legs == () and quote.net == ZERO


def test_missing_market_context_is_blocked_not_mispriced():
    empty = quote_lmsr_group(
        [], {1: Decimal("10")}, market_id=1, b=B, fee_rate=ZERO,
        mode="full", partial_pct=Decimal("1"),
    )
    assert empty.blocked_reason == "no_outcomes" and empty.net == ZERO

    unknown = quote_lmsr_group(
        [_snap(1, "100")], {99: Decimal("10")}, market_id=1, b=B, fee_rate=ZERO,
        mode="full", partial_pct=Decimal("1"),
    )
    assert unknown.blocked_reason == "unknown_outcome" and unknown.net == ZERO

    idle = quote_lmsr_group(
        [_snap(1, "100")], {}, market_id=1, b=B, fee_rate=ZERO,
        mode="full", partial_pct=Decimal("1"),
    )
    assert idle.blocked_reason is None and idle.net == ZERO and idle.legs == ()


def test_invalid_inputs_raise_value_error():
    outcomes = [_snap(1, "100")]
    cases = [
        dict(b=0.0, fee_rate=ZERO, mode="full", partial_pct=Decimal("1")),
        dict(b=B, fee_rate=Decimal("1"), mode="full", partial_pct=Decimal("1")),
        dict(b=B, fee_rate=Decimal("-0.1"), mode="full", partial_pct=Decimal("1")),
        dict(b=B, fee_rate=ZERO, mode="half", partial_pct=Decimal("1")),
        dict(b=B, fee_rate=ZERO, mode="partial", partial_pct=ZERO),
        dict(b=B, fee_rate=ZERO, mode="partial", partial_pct=Decimal("1.5")),
        dict(b=B, fee_rate=ZERO, mode="full", partial_pct=Decimal("1"), unit=ZERO),
    ]
    for kwargs in cases:
        with pytest.raises(ValueError):
            quote_lmsr_group(outcomes, {1: Decimal("10")}, market_id=1, **kwargs)

    with pytest.raises(ValueError):
        quote_lmsr_group(
            [_snap(1, "100"), _snap(1, "100")], {1: Decimal("10")}, market_id=1,
            b=B, fee_rate=ZERO, mode="full", partial_pct=Decimal("1"),
        )
    with pytest.raises(ValueError):
        quote_lmsr_group(
            outcomes, {1: Decimal("NaN")}, market_id=1, b=B, fee_rate=ZERO,
            mode="full", partial_pct=Decimal("1"),
        )
