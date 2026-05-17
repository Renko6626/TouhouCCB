"""与后端 backend/app/services/lmsr.py 公式对照。差异 < 1e-8 才通过。"""
import math

import pytest

from thccb_quant.lmsr import (
    calculate_lmsr_cost,
    get_current_price,
    calculate_lmsr_with_prices,
)


def _ref_cost(shares, b):
    if not shares:
        return 0.0
    max_q = max(shares)
    sum_exp = sum(math.exp((q - max_q) / b) for q in shares)
    return b * (math.log(sum_exp) + (max_q / b))


def _ref_price(shares, idx, b):
    max_q = max(shares)
    exps = [math.exp((q - max_q) / b) for q in shares]
    return exps[idx] / sum(exps)


@pytest.mark.parametrize(
    "shares,b",
    [
        ([0.0, 0.0], 100.0),
        ([10.0, 5.0], 100.0),
        ([100.0, 50.0], 100.0),
        ([1000.0, 800.0, 500.0], 100.0),
        ([0.5, 0.5], 50.0),
    ],
)
def test_cost_matches_backend(shares, b):
    assert abs(calculate_lmsr_cost(shares, b) - _ref_cost(shares, b)) < 1e-8


@pytest.mark.parametrize(
    "shares,b,idx",
    [
        ([10.0, 5.0], 100.0, 0),
        ([10.0, 5.0], 100.0, 1),
        ([1000.0, 800.0, 500.0], 100.0, 2),
    ],
)
def test_price_matches_backend(shares, b, idx):
    assert abs(get_current_price(shares, idx, b) - _ref_price(shares, idx, b)) < 1e-8


def test_empty_shares_returns_zero():
    assert calculate_lmsr_cost([], 100.0) == 0.0


def test_with_prices_combined():
    shares = [10.0, 5.0]
    b = 100.0
    cost, prices = calculate_lmsr_with_prices(shares, b)
    assert abs(cost - _ref_cost(shares, b)) < 1e-8
    assert abs(prices[0] - _ref_price(shares, 0, b)) < 1e-8
    assert abs(sum(prices) - 1.0) < 1e-9
