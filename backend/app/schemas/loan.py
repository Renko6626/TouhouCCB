"""Loan API 请求/响应模型。"""
from decimal import Decimal
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field, condecimal
from app.schemas.fx import FxShortPositionPublic


class BorrowRequest(BaseModel):
    amount: condecimal(gt=0, max_digits=16, decimal_places=6)


class RepayRequest(BaseModel):
    amount: condecimal(gt=0, max_digits=16, decimal_places=6)


class LoanQuotaResponse(BaseModel):
    enabled: bool
    cash: Decimal
    debt: Decimal
    available_cash: Optional[Decimal] = None
    restricted_cash: Decimal = Decimal("0")
    short_positions: list[FxShortPositionPublic] = []
    short_cover_cost: Optional[Decimal] = None
    risk_basis: Optional[Decimal] = None
    equity_to_risk_basis: Optional[float] = None
    risk_status: Optional[str] = None
    new_risk_frozen: bool = False
    borrow_blocked_reason: Optional[str] = None
    credit_frozen: bool = False
    economic_version: Optional[int] = None
    # 注意：本字段是 LCV 口径（含 LMSR 滑点 + 扣 sell_fee），与 /user/summary.net_worth
    # 的 MTM 口径不同！borrow/repay 路径走保守口径，避免 MTM 虚高估值导致过度杠杆。
    # 详见 docs/holdings-value-semantics.md。
    # 空头回补成本 K 无法完整报价时为 None：不得写 0，前端应展示 blocked_reason。
    net_worth: Optional[Decimal]
    leverage_k: Decimal
    daily_rate: Decimal
    max_borrow: Decimal
    last_accrued_at: Optional[datetime]
    display_equity: Optional[Decimal] = None
    liquidation_equity: Optional[Decimal] = None
    r_initial: Optional[Decimal] = None
    r_maintenance: Optional[Decimal] = None
    # K 未知 / 数据错误时的明确阻塞原因（spec §5.2/§10）；正常时为 None。
    blocked_reason: Optional[str] = None


class LoanActionResponse(BaseModel):
    cash: Decimal
    debt: Decimal
    max_borrow: Optional[Decimal] = None
    # 实际生效金额（仅 repay 有意义；borrow 永远等于请求金额）
    # 用户输入 amount > 真实 debt 或 > cash 时，effective 会被服务层封顶
    effective: Optional[Decimal] = None


class ForceLoanRequest(BaseModel):
    amount: condecimal(gt=0, max_digits=16, decimal_places=6)
    reason: str = Field(..., min_length=1, max_length=200)


class ForgiveDebtRequest(BaseModel):
    amount: condecimal(gt=0, max_digits=16, decimal_places=6)
    reason: str = Field(..., min_length=1, max_length=200)


class SiteConfigItem(BaseModel):
    key: str
    value: str
    value_type: str
    updated_at: datetime
    updated_by: Optional[int]


class SiteConfigUpdate(BaseModel):
    value: str = Field(..., min_length=1, max_length=200)
