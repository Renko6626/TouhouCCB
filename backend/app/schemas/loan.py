"""Loan API 请求/响应模型。"""
from decimal import Decimal
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field, condecimal


class BorrowRequest(BaseModel):
    amount: condecimal(gt=0, max_digits=16, decimal_places=6)


class RepayRequest(BaseModel):
    amount: condecimal(gt=0, max_digits=16, decimal_places=6)


class LoanQuotaResponse(BaseModel):
    enabled: bool
    cash: Decimal
    debt: Decimal
    net_worth: Decimal
    leverage_k: Decimal
    daily_rate: Decimal
    max_borrow: Decimal
    last_accrued_at: Optional[datetime]


class LoanActionResponse(BaseModel):
    cash: Decimal
    debt: Decimal
    max_borrow: Decimal


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
