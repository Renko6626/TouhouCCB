"""WP1 纯函数：credit/thresholds.py 的两条门槛、额度与旧 k 等价（F6/F7）。"""
import os
import random
import sys
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app.models.base import User
from app.services.credit.thresholds import (
    MAX_LEVERAGE,
    Q6,
    RiskThresholds,
    derive_thresholds,
    validate_thresholds,
)
from app.services.loan_service import compute_max_borrow

R_MAINT = Decimal("0.04")
R_INITIAL_20X = Decimal(1) / Decimal(19)


def test_derive_20x_uses_high_precision_reciprocal():
    t = derive_thresholds(Decimal("20"), R_MAINT)
    assert t.leverage == Decimal("20")
    assert t.r_maintenance == R_MAINT
    # prec=28 倒数：1/19 到 28 位有效数字
    assert t.r_initial == R_INITIAL_20X
    assert str(t.r_initial) == "0.05263157894736842105263157895"


def test_spec_example_max_borrow():
    """spec §4 示例：现金 500、债务 9500、可回收 10000 → E=1000，20x 下额度 9500。"""
    t = derive_thresholds(Decimal("20"), R_MAINT)
    assert t.max_borrow(Decimal("1000"), Decimal("9500")) == Decimal("9500.000000")


def test_max_borrow_floors_and_never_negative():
    t = derive_thresholds(Decimal("3"), Decimal("0.04"))   # r_initial = 0.5
    # E*(L-1) - D = 100.0000005 → 向下 6dp
    assert t.max_borrow(Decimal("50.00000025"), Decimal("0")) == Decimal("100.000000")
    assert t.max_borrow(Decimal("10"), Decimal("100")) == Decimal("0")
    assert t.max_borrow(Decimal("0"), Decimal("0")) == Decimal("0")


def test_max_borrow_uses_multiplication_not_reciprocal():
    """裁定：E*(leverage-1) - D，避免 1/R_initial 舍入把额度抬高。"""
    t = derive_thresholds(Decimal("19"), Decimal("0.04"))
    equity = Decimal("0.000001")
    expected = (equity * Decimal(18)).quantize(Q6)
    assert t.max_borrow(equity, Decimal("0")) == expected


def test_triggered_and_recovered_boundaries():
    t = derive_thresholds(Decimal("20"), R_MAINT)
    debt = Decimal("10000")
    # 触发线：E < 0.04*D == 400
    assert t.triggered(Decimal("399.999999"), debt) is True
    assert t.triggered(Decimal("400"), debt) is False
    assert t.triggered(Decimal("-1"), debt) is True
    # 无债不触发（spec §4：D=0 不做比例除法）
    assert t.triggered(Decimal("-100"), Decimal("0")) is False

    # 恢复线：E >= R_initial*D
    recovery_line = t.r_initial * debt
    assert t.recovered(recovery_line, debt) is True
    assert t.recovered(recovery_line - Decimal("0.000001"), debt) is False
    assert t.recovered(Decimal("-50"), Decimal("0")) is True


def test_validate_rejects_illegal_thresholds():
    validate_thresholds(Decimal("20"), R_MAINT)
    validate_thresholds(Decimal("1.000001"), Decimal("0.000001"))

    for lev, maint in [
        (Decimal("1"), R_MAINT),            # R_initial 无定义
        (Decimal("0.5"), R_MAINT),
        (Decimal("21"), R_MAINT),           # > 20x 上限
        (MAX_LEVERAGE + Decimal("0.000001"), R_MAINT),
        (Decimal("20"), Decimal("0")),      # 维持率必须 > 0
        (Decimal("20"), Decimal("-0.01")),
        (Decimal("2"), Decimal("1")),       # == R_initial(2)=1
        (Decimal("2"), Decimal("1.5")),     # > R_initial
        (Decimal("NaN"), R_MAINT),
        (Decimal("20"), Decimal("NaN")),
        (Decimal("20"), Decimal("Infinity")),
    ]:
        with pytest.raises(ValueError):
            validate_thresholds(lev, maint)

    # maintenance == R_initial 必须拒绝（用派生出的精确倒数，不靠硬编码小数）
    exact = derive_thresholds(Decimal("20"), R_MAINT)
    with pytest.raises(ValueError):
        validate_thresholds(Decimal("20"), exact.r_initial)


def test_derive_rejects_invalid_and_accepts_float_str():
    with pytest.raises(ValueError):
        derive_thresholds(Decimal("20"), Decimal("0.1"))   # 0.1 > 1/19
    t = derive_thresholds("4", "0.04")
    assert isinstance(t, RiskThresholds)
    assert t.r_initial == Decimal(1) / Decimal(3)


def test_maintenance_must_be_below_r_initial_for_every_valid_leverage():
    for lev_int in range(2, 21):
        t = derive_thresholds(Decimal(lev_int), R_MAINT)
        assert t.r_maintenance < t.r_initial


def _q6(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Q6)


def test_legacy_equivalence_random_100_integer_k():
    """F7：credit_leverage = k+1 时，旧 compute_max_borrow 与新 max_borrow 完全一致。

    整数 k（2..11 倍杠杆）时 k*net 恰好 6dp，两种量化规则结果相同 → 严格相等。
    """
    rng = random.Random(20260930)
    for _ in range(100):
        k = Decimal(rng.randint(1, 10))
        cash = _q6(rng.uniform(0, 10000))
        debt = _q6(rng.uniform(0, 10000))
        holdings = _q6(rng.uniform(0, 10000))
        user = User(username="eq", cash=cash, debt=debt)
        legacy = compute_max_borrow(user, holdings, k)
        new = derive_thresholds(k + Decimal(1), R_MAINT).max_borrow(
            cash + holdings - debt, debt
        )
        assert new == legacy, (k, cash, debt, holdings, legacy, new)


def test_legacy_equivalence_fractional_k_never_loosens_credit():
    """小数 k 时乘积超出 6dp：新额度向下量化，最多比旧值少 1e-6，绝不更多。"""
    rng = random.Random(7)
    for _ in range(100):
        k = _q6(rng.uniform(0.01, 10))
        cash = _q6(rng.uniform(0, 10000))
        debt = _q6(rng.uniform(0, 10000))
        holdings = _q6(rng.uniform(0, 10000))
        user = User(username="eq", cash=cash, debt=debt)
        legacy = compute_max_borrow(user, holdings, k)
        new = derive_thresholds(k + Decimal(1), R_MAINT).max_borrow(
            cash + holdings - debt, debt
        )
        assert Decimal("0") <= legacy - new <= Q6, (k, legacy, new)


def test_risk_basis_max_of_debt_and_alpha_assets_plus_alpha_short():
    """B = max(D, αA) + αK：多头资产项与空头成本共享风险基数，不按方向抵消。"""
    t = derive_thresholds(Decimal("10"), R_MAINT)  # alpha = 0.9
    assert t.risk_basis(
        debt=Decimal("4000"), positive_assets=Decimal("100"),
        short_cover=Decimal("500"),
    ) == Decimal("4000") + Decimal("0.9") * Decimal("500")
    # alpha*A 占主导时用资产项
    assert t.risk_basis(
        debt=Decimal("1"), positive_assets=Decimal("10000"),
        short_cover=Decimal("500"),
    ) == Decimal("0.9") * Decimal("10000") + Decimal("0.9") * Decimal("500")
    # 无空头且无资产时退化为旧 D
    assert t.risk_basis(
        debt=Decimal("123.456789"), positive_assets=Decimal("0"),
        short_cover=Decimal("0"),
    ) == Decimal("123.456789")
    with pytest.raises(ValueError):
        t.risk_basis(
            debt=Decimal("NaN"), positive_assets=Decimal("0"),
            short_cover=Decimal("0"),
        )


def test_basis_measure_and_admission_use_exact_w_form():
    """spec §6.1: W = max(LD,(L−1)A) + (L−1)K decides admission, not quantized B."""
    t = derive_thresholds(Decimal("10"), R_MAINT)   # alpha = 0.9
    debt, assets, cover = Decimal("4500"), Decimal("5000"), Decimal("5000")
    basis, w = t.basis_measure(
        debt=debt, positive_assets=assets, short_cover=cover,
    )
    assert basis == max(debt, t.alpha * assets) + t.alpha * cover
    assert w == (
        max(t.leverage * debt, (t.leverage - 1) * assets)
        + (t.leverage - 1) * cover
    )
    # spec §6.2 "同本金各开满 10 倍多与空" boundary: W=90000, E=1000.
    boundary_e = w / (t.leverage * (t.leverage - 1))
    assert boundary_e == Decimal("1000")
    assert t.admits(equity=boundary_e, debt=debt,
                    positive_assets=assets, short_cover=cover) is True
    assert t.admits(equity=boundary_e - Q6, debt=debt,
                    positive_assets=assets, short_cover=cover) is False


def test_max_new_gold_loan_subtracts_alpha_short_share_and_stays_zero():
    t = derive_thresholds(Decimal("10"), R_MAINT)
    # E=500, D=0, A=0, K=100: admitted, headroom = (L-1)E - D - alpha*K = 4410.
    assert t.max_new_gold_loan(
        equity=Decimal("500"), debt=Decimal("0"),
        positive_assets=Decimal("0"), short_cover=Decimal("100"),
    ) == Decimal("4410.000000")
    # Below initial margin: zero, never a negative/normalized number.
    assert t.max_new_gold_loan(
        equity=Decimal("0"), debt=Decimal("0"),
        positive_assets=Decimal("0"), short_cover=Decimal("100"),
    ) == Decimal("0")
    # D already exceeds the available basis: zero.
    assert t.max_new_gold_loan(
        equity=Decimal("100"), debt=Decimal("1000"),
        positive_assets=Decimal("0"), short_cover=Decimal("0"),
    ) == Decimal("0")
    # Floors down at 6dp without ever rounding up.
    assert t.max_new_gold_loan(
        equity=Decimal("1.0000001"), debt=Decimal("0"),
        positive_assets=Decimal("0"), short_cover=Decimal("0"),
    ) == Decimal("9.000000")


def test_triggered_basis_requires_debt_or_short_and_uses_w():
    t = derive_thresholds(Decimal("10"), R_MAINT)   # m = 0.04
    debt, cover, assets = Decimal("4500"), Decimal("5000"), Decimal("0")
    # W = 90000, trigger line L*E < m*W = 3600 -> E < 360.
    assert t.triggered_basis(
        equity=Decimal("359.999999"), debt=debt,
        positive_assets=assets, short_cover=cover,
    ) is True
    assert t.triggered_basis(
        equity=Decimal("360"), debt=debt,
        positive_assets=assets, short_cover=cover,
    ) is False
    # No gold debt and no foreign obligation: never triggered.
    assert t.triggered_basis(
        equity=Decimal("-1000"), debt=Decimal("0"),
        positive_assets=Decimal("0"), short_cover=Decimal("0"),
    ) is False
    # Foreign-only debt can trigger.
    assert t.triggered_basis(
        equity=Decimal("0"), debt=Decimal("0"),
        positive_assets=Decimal("0"), short_cover=cover,
    ) is True
