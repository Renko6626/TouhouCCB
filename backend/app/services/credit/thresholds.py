"""统一风险门槛（spec 2026-09-29 §4；计划 §3.2 冻结签名，纯函数）。

只有两条门槛，所有产品共用：

    R_initial > R_maintenance > 0
    有债账户增险后：E_after >= R_initial * D_after
    强平触发：     D > 0 且 E < R_maintenance * D
    强平停止：     D == 0 或 E >= R_initial * D

``R_initial = 1 / (leverage - 1)``，与旧 ``loan_leverage_k`` 的等价映射是
``credit_leverage = k + 1``（F7：迁移不放松授信）。这里保存的是名义杠杆
Leverage，计算时用 prec=28 的倒数，不把截断小数当精确边界。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, localcontext
from typing import Union

Q6 = Decimal("0.000001")
#: 名义杠杆上限（计划 §3.4 / F6：20x 需运营显式启用，代码只做上限校验）。
MAX_LEVERAGE = Decimal("20")
#: R_initial 倒数精度（spec §4："保存名义杠杆并在计算中使用高精度倒数"）。
PREC = 28

Number = Union[Decimal, int, str]


def _finite_decimal(value: Number, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} 不是合法 Decimal: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} 必须是有限 Decimal: {value!r}")
    return parsed


@dataclass(frozen=True)
class RiskThresholds:
    """派生后的门槛三元组；``r_initial`` / ``r_maintenance`` 是比率（非金额）。"""

    leverage: Decimal
    r_initial: Decimal
    r_maintenance: Decimal

    def triggered(self, equity: Number, debt: Number) -> bool:
        """强平触发：``debt > 0 且 equity < r_maintenance*debt``（spec §4）。"""
        equity = _finite_decimal(equity, "equity")
        debt = _finite_decimal(debt, "debt")
        return debt > 0 and equity < self.r_maintenance * debt

    def recovered(self, equity: Number, debt: Number) -> bool:
        """恢复目标：``debt == 0 或 equity >= r_initial*debt``（初始门槛兼作恢复门槛）。"""
        equity = _finite_decimal(equity, "equity")
        debt = _finite_decimal(debt, "debt")
        return debt == 0 or equity >= self.r_initial * debt

    def max_borrow(self, equity: Number, debt: Number) -> Decimal:
        """新增借款额度 ``max(0, E/R_initial - D)``，6dp **向下**量化。

        ``1/R_initial == leverage - 1``，所以直接用乘法
        ``E*(leverage-1) - D``：与 ``E/r_initial`` 代数等价，但避免先把
        倒数舍入到 prec=28 再相乘造成的额度高估（主 agent 裁定）。
        """
        equity = _finite_decimal(equity, "equity")
        debt = _finite_decimal(debt, "debt")
        headroom = equity * (self.leverage - Decimal(1)) - debt
        if headroom <= 0:
            return Decimal("0")
        # 不放宽授信：非整除时向下取到 6dp（与旧 compute_max_borrow 的 HALF_EVEN
        # 最多差 1e-6，方向永远是"更严"）。
        return headroom.quantize(Q6, rounding=ROUND_FLOOR)


def validate_thresholds(leverage: Number, maintenance: Number) -> None:
    """校验配置合法性；非法抛 ``ValueError``（计划 §3.2）。

    - ``1 < leverage <= 20``（20x 是目标上限，见 F6）
    - ``0 < maintenance < r_initial``（维持率必须严格小于初始率）
    """
    lev = _finite_decimal(leverage, "leverage")
    maint = _finite_decimal(maintenance, "maintenance")
    if lev <= Decimal(1):
        raise ValueError(f"leverage 必须 > 1（R_initial 无定义）: {lev}")
    if lev > MAX_LEVERAGE:
        raise ValueError(f"leverage 超过上限 {MAX_LEVERAGE}: {lev}")
    if maint <= 0:
        raise ValueError(f"maintenance 必须 > 0: {maint}")
    with localcontext() as ctx:
        ctx.prec = PREC
        r_initial = Decimal(1) / (lev - Decimal(1))
    if maint >= r_initial:
        raise ValueError(
            f"maintenance({maint}) 必须 < R_initial({r_initial})，否则初始门槛不严于维持门槛"
        )


def derive_thresholds(leverage: Number, maintenance: Number) -> RiskThresholds:
    """校验并派生门槛；``r_initial`` 用 prec=28 倒数（计划 §3.2）。"""
    validate_thresholds(leverage, maintenance)
    lev = Decimal(leverage)
    with localcontext() as ctx:
        ctx.prec = PREC
        r_initial = Decimal(1) / (lev - Decimal(1))
    return RiskThresholds(
        leverage=lev,
        r_initial=r_initial,
        r_maintenance=Decimal(maintenance),
    )
