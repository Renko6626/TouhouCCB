"""Title 系统 Pydantic 请求/响应 schema。"""
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


# ── 公共：title 简化视图（chip 渲染用） ────────
class TitleChipRead(BaseModel):
    id: int
    name: str
    color: str
    icon: str


# ── catalog 视图 ────────────────────────────
class TitleRead(BaseModel):
    id: int
    name: str
    description: str
    color: str
    icon: str
    sort_order: int
    is_active: bool
    created_at: datetime


class TitleCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=32)
    description: str = Field(default="", max_length=200)
    color: str = Field(default="#000000", max_length=16)
    icon: str = Field(default="", max_length=16)
    sort_order: int = Field(default=100)


class TitleUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=32)
    description: Optional[str] = Field(default=None, max_length=200)
    color: Optional[str] = Field(default=None, max_length=16)
    icon: Optional[str] = Field(default=None, max_length=16)
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


# ── 用户自助 ─────────────────────────────────
class MyTitleItem(BaseModel):
    title: TitleRead
    granted_at: datetime
    source: str


class MyTitlesResponse(BaseModel):
    equipped_title_id: Optional[int]
    titles: List[MyTitleItem]


class EquipRequest(BaseModel):
    title_id: Optional[int] = Field(default=None, description="null = 取下")


class RedeemRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=64)


class RedeemResponse(BaseModel):
    title: TitleRead


# ── Batch / Code ──────────────────────────────
class BatchCreateRequest(BaseModel):
    title_id: int
    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(default="", max_length=200)


class BatchRead(BaseModel):
    id: int
    title_id: int
    title_name: str
    name: str
    description: str
    total: int
    used: int
    created_at: datetime


class CodeRead(BaseModel):
    id: int
    code_string: str
    status: str
    used_by_username: Optional[str]
    used_at: Optional[datetime]


class CSVImportResponse(BaseModel):
    inserted: int


# ── Admin user-title ──────────────────────────
class UserTitleGrantRequest(BaseModel):
    title_id: int


class UserTitleListItem(BaseModel):
    title: TitleRead
    granted_at: datetime
    source: str
    granted_by_admin_id: Optional[int]


# ── Market required title ────────────────────
class MarketRequiredTitlesPutRequest(BaseModel):
    title_ids: List[int] = Field(default_factory=list)
