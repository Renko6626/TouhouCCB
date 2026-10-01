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
#: 统一信贷名义杠杆上限；实际倍数仍需运营显式配置。
MAX_LEVERAGE = Decimal("50")
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

    @property
    def alpha(self) -> Decimal:
        """空头共享风险系数 ``α = (L−1)/L``（spec §6.1），prec=28 计算。"""
        with localcontext() as ctx:
            ctx.prec = PREC
            return (self.leverage - Decimal(1)) / self.leverage

    def risk_basis(
        self,
        *,
        debt: Number,
        positive_assets: Number,
        short_cover: Number,
    ) -> Decimal:
        """共享风险基数 ``B = max(D, α×A) + α×K``（spec §6.1）。

        B 是风险基数而非债务余额：真实负债仍是 D 与各币种欠币数量。未知 K 不
        得传 0 进来伪装成已知；调用方在 K 未知时整体把 E/B 标为未知。金额
        量化（保守方向）由展示层负责，这里保持高精度。
        """
        debt_value = _finite_decimal(debt, "debt")
        assets_value = _finite_decimal(positive_assets, "positive_assets")
        cover_value = _finite_decimal(short_cover, "short_cover")
        alpha = self.alpha
        return max(debt_value, alpha * assets_value) + alpha * cover_value

    def basis_measure(
        self,
        *,
        debt: Number,
        positive_assets: Number,
        short_cover: Number,
    ) -> tuple[Decimal, Decimal]:
        """返回 ``(B, W)``；``B`` 仅供展示，准入比较必须用未截断的 ``W``。

        ``W = max(L×D, (L−1)×A) + (L−1)×K`` 与 ``B`` 同源（``W = L×B``），
        但直接用乘法比较避免先算 ``B = W/L`` 再取倒数的舍入放宽授信。
        """
        debt_value = _finite_decimal(debt, "debt")
        assets_value = _finite_decimal(positive_assets, "positive_assets")
        cover_value = _finite_decimal(short_cover, "short_cover")
        one = Decimal(1)
        alpha = self.alpha
        basis = max(debt_value, alpha * assets_value) + alpha * cover_value
        w = (
            max(self.leverage * debt_value, (self.leverage - one) * assets_value)
            + (self.leverage - one) * cover_value
        )
        return basis, w

    def admits(
        self,
        *,
        equity: Number,
        debt: Number,
        positive_assets: Number,
        short_cover: Number,
    ) -> bool:
        """新增风险准入：``L×(L−1)×E >= W``（spec §6.1，用未量化 E 比较）。"""
        equity_value = _finite_decimal(equity, "equity")
        _, w = self.basis_measure(
            debt=debt, positive_assets=positive_assets, short_cover=short_cover,
        )
        return self.leverage * (self.leverage - Decimal(1)) * equity_value >= w

    def triggered_basis(
        self,
        *,
        equity: Number,
        debt: Number,
        positive_assets: Number,
        short_cover: Number,
    ) -> bool:
        """强平触发：有金债或欠币时 ``L×E < m×W``；无任何负债不触发。"""
        equity_value = _finite_decimal(equity, "equity")
        debt_value = _finite_decimal(debt, "debt")
        cover_value = _finite_decimal(short_cover, "short_cover")
        if debt_value <= 0 and cover_value <= 0:
            return False
        _, w = self.basis_measure(
            debt=debt_value, positive_assets=positive_assets, short_cover=cover_value,
        )
        return self.leverage * equity_value < self.r_maintenance * w

    def max_new_gold_loan(
        self,
        *,
        equity: Number,
        debt: Number,
        positive_assets: Number,
        short_cover: Number,
    ) -> Decimal:
        """现金借款理论额度 ``max(0, (L−1)E − D − αK)``，6dp 向下量化。

        当前已低于初始门槛（或用未知 K 无法判定）时为零。它不是可开仓数量或
        可消费额度：新买入还会增加 A，必须另做交易后检查（spec §6.3）。
        """
        equity_value = _finite_decimal(equity, "equity")
        debt_value = _finite_decimal(debt, "debt")
        cover_value = _finite_decimal(short_cover, "short_cover")
        admitted = self.admits(
            equity=equity_value, debt=debt_value,
            positive_assets=positive_assets, short_cover=cover_value,
        )
        if not admitted:
            return Decimal("0")
        headroom = (
            (self.leverage - Decimal(1)) * equity_value
            - debt_value - self.alpha * cover_value
        )
        if headroom <= 0:
            return Decimal("0")
        return headroom.quantize(Q6, rounding=ROUND_FLOOR)


def validate_thresholds(leverage: Number, maintenance: Number) -> None:
    """校验配置合法性；非法抛 ``ValueError``（计划 §3.2）。

    - ``1 < leverage <= 50``
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
