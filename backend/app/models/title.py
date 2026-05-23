"""Title 系统的 5 张表 + User.equipped_title_id 列定义。

CLAUDE.md 性能护栏：所有反向 List[...] 关系一律 lazy="raise_on_sql"。
读 user.user_titles / market.required_titles 时必须显式 selectinload。
"""
from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import (
    UniqueConstraint, CheckConstraint, Index, DateTime,
)


class Title(SQLModel, table=True):
    __tablename__ = "title"
    __table_args__ = (UniqueConstraint("name", name="uq_title_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(nullable=False, max_length=32, index=True)
    description: str = Field(default="", max_length=200, nullable=False)
    color: str = Field(default="#000000", max_length=16, nullable=False)
    icon: str = Field(default="", max_length=16, nullable=False)
    sort_order: int = Field(default=100, index=True)
    is_active: bool = Field(default=True, nullable=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )


class UserTitle(SQLModel, table=True):
    __tablename__ = "user_title"
    __table_args__ = (
        UniqueConstraint("user_id", "title_id", name="uq_user_title"),
        CheckConstraint("source IN ('admin','code')", name="ck_user_title_source"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True, nullable=False)
    title_id: int = Field(foreign_key="title.id", index=True, nullable=False)
    granted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
    granted_by_admin_id: Optional[int] = Field(default=None, foreign_key="user.id")
    source: str = Field(max_length=16, nullable=False)  # 'admin' | 'code'


class TitleCodeBatch(SQLModel, table=True):
    __tablename__ = "title_code_batch"

    id: Optional[int] = Field(default=None, primary_key=True)
    title_id: int = Field(foreign_key="title.id", index=True, nullable=False)
    name: str = Field(nullable=False, max_length=64)
    description: str = Field(default="", max_length=200, nullable=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
    )
    created_by_admin_id: Optional[int] = Field(default=None, foreign_key="user.id")


class TitleCode(SQLModel, table=True):
    __tablename__ = "title_code"
    __table_args__ = (
        UniqueConstraint("code_string", name="uq_title_code_string"),
        CheckConstraint("status IN ('available','used')", name="ck_title_code_status"),
        Index("ix_title_code_batch_status", "batch_id", "status"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: int = Field(foreign_key="title_code_batch.id", index=True, nullable=False)
    code_string: str = Field(nullable=False, max_length=64)
    status: str = Field(default="available", nullable=False)  # 'available' | 'used'
    used_by_user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    used_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


class MarketRequiredTitle(SQLModel, table=True):
    """市场 → 允许交易的 title 名单（ANY-of）。空 = 任何人可交易。"""
    __tablename__ = "market_required_title"
    __table_args__ = (
        UniqueConstraint("market_id", "title_id", name="uq_market_required_title"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    market_id: int = Field(foreign_key="market.id", index=True, nullable=False)
    title_id: int = Field(foreign_key="title.id", index=True, nullable=False)
