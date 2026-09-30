"""Persistent storage for the FX sub-game.

The FX tables deliberately do not reuse LMSR market tables.  All quantities are
stored at six decimal places; hidden event controls stay in the event table and
are only exposed by administrator schemas.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from sqlalchemy import CheckConstraint, Column, Date, DateTime, Index, JSON, Numeric, UniqueConstraint, text
from sqlmodel import Field, SQLModel


class FxPairStatus(str, Enum):
    DRAFT = "draft"
    TRADING = "trading"
    PAUSED = "paused"
    CLOSED = "closed"


class FxEventStatus(str, Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FxPair(SQLModel, table=True):
    __tablename__ = "fx_pair"
    __table_args__ = (
        CheckConstraint("status IN ('draft','trading','paused','closed')", name="ck_fx_pair_status"),
        CheckConstraint("gold_reserve > 0 AND foreign_reserve > 0", name="ck_fx_pair_reserves_positive"),
        CheckConstraint("target_price > 0 AND target_min > 0 AND target_max > 0", name="ck_fx_pair_targets_positive"),
        CheckConstraint("target_min <= target_price AND target_price <= target_max", name="ck_fx_pair_target_range"),
        CheckConstraint("buy_fee_rate >= 0 AND buy_fee_rate <= 1 AND sell_fee_rate >= 0 AND sell_fee_rate <= 1", name="ck_fx_pair_fee_rate"),
        Index("ix_fx_pair_status", "status"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    currency_code: str = Field(max_length=16, nullable=False, unique=True)
    currency_name: str = Field(max_length=64, nullable=False)
    status: str = Field(default=FxPairStatus.DRAFT.value, max_length=16, nullable=False)
    archived: bool = Field(default=False, nullable=False, sa_column_kwargs={"server_default": text("false")})
    gold_reserve: Decimal = Field(default=Decimal("1"), sa_type=Numeric(16, 6), nullable=False)
    foreign_reserve: Decimal = Field(default=Decimal("1"), sa_type=Numeric(16, 6), nullable=False)
    target_price: Decimal = Field(default=Decimal("1"), sa_type=Numeric(16, 6), nullable=False)
    initial_price: Decimal = Field(default=Decimal("1"), sa_type=Numeric(16, 6), nullable=False)
    target_min: Decimal = Field(default=Decimal("0.5"), sa_type=Numeric(16, 6), nullable=False)
    target_max: Decimal = Field(default=Decimal("2"), sa_type=Numeric(16, 6), nullable=False)
    buy_fee_rate: Decimal = Field(default=Decimal("0"), sa_type=Numeric(10, 8), nullable=False)
    sell_fee_rate: Decimal = Field(default=Decimal("0"), sa_type=Numeric(10, 8), nullable=False)
    pool_version: int = Field(default=1, nullable=False)
    # ── 统一信贷风险 F9：paused/reduce_only 双轴语义 ──
    # reduce_only=True：只允许卖出/强平，拒绝开仓与系统干预（L 仍按可执行报价计算）。
    # 默认 false：paused 仍是"全停"旧语义，迁移不得把已 paused 的 pair 无提示变成可卖。
    reduce_only: bool = Field(
        default=False, nullable=False, sa_column_kwargs={"server_default": text("false")},
    )
    created_at: datetime = Field(default_factory=_utcnow, sa_type=DateTime(timezone=True), nullable=False)
    updated_at: datetime = Field(default_factory=_utcnow, sa_type=DateTime(timezone=True), nullable=False)


class FxTreasury(SQLModel, table=True):
    __tablename__ = "fx_treasury"
    __table_args__ = (
        UniqueConstraint("pair_id", name="uq_fx_treasury_pair"),
        CheckConstraint("gold_balance >= 0 AND foreign_balance >= 0 AND daily_spend >= 0", name="ck_fx_treasury_balances_non_negative"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    pair_id: int = Field(foreign_key="fx_pair.id", nullable=False, index=True)
    gold_balance: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    foreign_balance: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    daily_spend: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    spend_date: Optional[date] = Field(default=None, sa_type=Date())
    updated_at: datetime = Field(default_factory=_utcnow, sa_type=DateTime(timezone=True), nullable=False)


class FxWallet(SQLModel, table=True):
    __tablename__ = "fx_wallet"
    __table_args__ = (UniqueConstraint("user_id", "pair_id", name="uq_fx_wallet_user_pair"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    pair_id: int = Field(foreign_key="fx_pair.id", nullable=False, index=True)
    foreign_amount: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    cost_basis: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    updated_at: datetime = Field(default_factory=_utcnow, sa_type=DateTime(timezone=True), nullable=False)


class FxTrade(SQLModel, table=True):
    __tablename__ = "fx_trade"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_fx_trade_user_idempotency"),
        CheckConstraint("side IN ('buy','sell')", name="ck_fx_trade_side"),
        CheckConstraint("input_amount > 0 AND output_amount > 0", name="ck_fx_trade_amounts_positive"),
        Index("ix_fx_trade_pair_created", "pair_id", "created_at"),
        Index("ix_fx_trade_pair_id", "pair_id"),
        Index("ix_fx_trade_user_id", "user_id"),
        Index("ix_fx_trade_created_at", "created_at"),
        CheckConstraint("pre_gold_reserve > 0 AND pre_foreign_reserve > 0 AND post_gold_reserve > 0 AND post_foreign_reserve > 0", name="ck_fx_trade_reserves_positive"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    pair_id: int = Field(foreign_key="fx_pair.id", nullable=False)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    side: str = Field(max_length=8, nullable=False)
    input_amount: Decimal = Field(sa_type=Numeric(16, 6), nullable=False)
    output_amount: Decimal = Field(sa_type=Numeric(16, 6), nullable=False)
    # Request identity used for safe idempotency replay.  Public trade schemas
    # deliberately omit this internal execution parameter.
    min_out: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    fee_amount: Decimal = Field(default=Decimal("0"), sa_type=Numeric(16, 6), nullable=False)
    pre_gold_reserve: Decimal = Field(sa_type=Numeric(16, 6), nullable=False)
    pre_foreign_reserve: Decimal = Field(sa_type=Numeric(16, 6), nullable=False)
    post_gold_reserve: Decimal = Field(sa_type=Numeric(16, 6), nullable=False)
    post_foreign_reserve: Decimal = Field(sa_type=Numeric(16, 6), nullable=False)
    post_price: Decimal = Field(sa_type=Numeric(16, 6), nullable=False)
    source: str = Field(default="player", max_length=24, nullable=False)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    created_at: datetime = Field(default_factory=_utcnow, sa_type=DateTime(timezone=True), nullable=False)


class FxEvent(SQLModel, table=True):
    __tablename__ = "fx_event"
    __table_args__ = (
        CheckConstraint("status IN ('draft','scheduled','published','cancelled','completed')", name="ck_fx_event_status"),
        Index("ix_fx_event_pair_status", "pair_id", "status"),
        Index("ix_fx_event_pair_id", "pair_id"),
        Index("ix_fx_event_status", "status"),
        Index("ix_fx_event_scheduled_at", "scheduled_at"),
        Index("ix_fx_event_operator_user_id", "operator_user_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    pair_id: int = Field(foreign_key="fx_pair.id", nullable=False)
    status: str = Field(default=FxEventStatus.DRAFT.value, max_length=16, nullable=False)
    title: str = Field(max_length=200, nullable=False)
    body: str = Field(default="", max_length=5000, nullable=False)
    kind: str = Field(max_length=32, nullable=False)
    shock_ratio: Optional[Decimal] = Field(default=None, sa_type=Numeric(10, 8))
    first_reaction_ratio: Optional[Decimal] = Field(default=None, sa_type=Numeric(10, 8))
    window_sec: Optional[int] = Field(default=None)
    budget: Optional[Decimal] = Field(default=None, sa_type=Numeric(16, 6))
    scheduled_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    published_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    completed_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    parameter_snapshot: Optional[dict] = Field(default=None, sa_column=Column(JSON, nullable=True))
    error_message: Optional[str] = Field(default=None, max_length=1000)
    operator_user_id: Optional[int] = Field(default=None, foreign_key="user.id")
