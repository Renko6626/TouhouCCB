"""兑换码模块数据模型：合作方 / 批次 / 单个码。

仅新增三张表，不改 base.py 已有列（CLAUDE.md 红线：base.py 没有迁移机制）。
依赖 SQLModel.metadata.create_all 自动建表，需在 app.main 顶部 import 本模块
触发 SQLModel 注册。
"""
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from sqlmodel import SQLModel, Field
from sqlalchemy import DateTime, Numeric, UniqueConstraint, Index


class BatchStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class CodeStatus(str, Enum):
    AVAILABLE = "available"
    SOLD = "sold"


class RedemptionPartner(SQLModel, table=True):
    __tablename__ = "redemption_partner"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, nullable=False)
    description: str = Field(default="")
    website_url: str = Field(default="")
    logo_url: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True, nullable=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )


class RedemptionBatch(SQLModel, table=True):
    __tablename__ = "redemption_batch"

    id: Optional[int] = Field(default=None, primary_key=True)
    partner_id: int = Field(foreign_key="redemption_partner.id", index=True, nullable=False)
    name: str = Field(nullable=False)
    description: str = Field(default="")
    unit_price: Decimal = Field(sa_type=Numeric(16, 6), nullable=False)
    status: str = Field(default=BatchStatus.DRAFT, index=True, nullable=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
    created_by_admin_id: Optional[int] = Field(default=None, foreign_key="user.id")


class RedemptionCode(SQLModel, table=True):
    __tablename__ = "redemption_code"
    __table_args__ = (
        UniqueConstraint("code_string", name="uq_redemption_code_string"),
        Index("ix_redemption_code_batch_status", "batch_id", "status"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: int = Field(foreign_key="redemption_batch.id", index=True, nullable=False)
    code_string: str = Field(nullable=False, max_length=128)
    status: str = Field(default=CodeStatus.AVAILABLE, nullable=False)
    bought_by_user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    bought_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    marked_used_by_user_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
