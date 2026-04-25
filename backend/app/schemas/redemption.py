"""兑换码模块 Pydantic schemas（请求/响应）。"""
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from pydantic import BaseModel, Field


# ========== 用户端 ==========

class PartnerPublic(BaseModel):
    id: int
    name: str
    description: str
    website_url: str
    logo_url: Optional[str]


class BatchListItem(BaseModel):
    id: int
    partner: PartnerPublic
    name: str
    unit_price: Decimal
    available_count: int


class BatchDetailResponse(BatchListItem):
    description: str


class PurchaseRequest(BaseModel):
    batch_id: int


class PurchaseResponse(BaseModel):
    code_id: int
    code_string: str
    batch_name: str
    partner_name: str
    partner_website_url: str
    description: str
    paid_amount: Decimal
    cash_after: Decimal


class MyRedemptionItem(BaseModel):
    code_id: int
    batch_name: str
    partner_name: str
    partner_website_url: str
    paid_amount: Decimal
    bought_at: datetime
    marked_used_by_user_at: Optional[datetime]


class MyRedemptionDetail(MyRedemptionItem):
    code_string: str
    description: str


class MarkUsedRequest(BaseModel):
    used: bool  # True=标记已用；False=取消标记


# ========== 管理员端 ==========

class PartnerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    website_url: str = ""
    logo_url: Optional[str] = None
    is_active: bool = True


class PartnerUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = None
    website_url: Optional[str] = None
    logo_url: Optional[str] = None
    is_active: Optional[bool] = None


class PartnerAdminItem(BaseModel):
    id: int
    name: str
    description: str
    website_url: str
    logo_url: Optional[str]
    is_active: bool
    created_at: datetime


class BatchCreate(BaseModel):
    partner_id: int
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    unit_price: Decimal = Field(gt=Decimal("0"))


class BatchUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    unit_price: Optional[Decimal] = Field(default=None, gt=Decimal("0"))
    status: Optional[str] = None  # draft/active/archived


class BatchAdminItem(BaseModel):
    id: int
    partner_id: int
    partner_name: str
    name: str
    description: str
    unit_price: Decimal
    status: str
    total_count: int
    sold_count: int
    available_count: int
    created_at: datetime


class CsvImportRequest(BaseModel):
    csv_text: str  # 整个 CSV 文本；前端可来自文件读取或粘贴


class CsvImportPreview(BaseModel):
    total_lines: int
    new_codes: List[str]          # 即将插入
    duplicate_codes: List[str]    # 与已有重复（跳过）
    invalid_codes: List[str]      # 非法（空/超长）


class CsvImportConfirm(BaseModel):
    csv_text: str
    confirm: bool = True  # 必须 True 才真正写入


class CsvImportResult(BaseModel):
    inserted: int
    skipped_duplicate: int
    skipped_invalid: int
