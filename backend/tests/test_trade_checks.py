from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.services.trade_checks import check_buy_slippage, check_sell_slippage

D = Decimal


def test_buy_max_cost_breach():
    with pytest.raises(HTTPException) as ei:
        check_buy_slippage(D("11"), D("10"), D("0.5"), D("10.5"), None, False)
    assert "max_cost" in ei.value.detail


def test_buy_bps_default_500_pass_and_fail():
    # expected=10，默认 5% 上限 = 10.5
    check_buy_slippage(D("10.5"), D("10"), D("0.5"), None, None, False)   # 恰好不超，通过
    with pytest.raises(HTTPException):
        check_buy_slippage(D("10.500001"), D("10"), D("0.5"), None, None, False)


def test_buy_bps_hardcap_1000():
    # 客户端要 5000bps，被截到 1000 → 上限 11
    check_buy_slippage(D("11"), D("10"), D("0.5"), None, 5000, False)
    with pytest.raises(HTTPException):
        check_buy_slippage(D("11.000001"), D("10"), D("0.5"), None, 5000, False)


def test_buy_accept_any_skips_bps_but_not_max_cost():
    check_buy_slippage(D("999"), D("10"), D("0.5"), None, None, True)
    with pytest.raises(HTTPException):
        check_buy_slippage(D("999"), D("10"), D("0.5"), D("100"), None, True)


def test_sell_min_proceeds_compares_net():
    with pytest.raises(HTTPException) as ei:
        check_sell_slippage(D("10"), D("9.5"), D("10"), D("0.5"), D("9.6"), None, False)
    assert "min_proceeds" in ei.value.detail


def test_sell_bps_compares_gross():
    # expected=10，默认 5% 下限 = 9.5；proceeds（gross）达标即可，net 低于无妨
    check_sell_slippage(D("9.5"), D("9"), D("10"), D("0.5"), None, None, False)
    with pytest.raises(HTTPException):
        check_sell_slippage(D("9.499999"), D("9"), D("10"), D("0.5"), None, None, False)
