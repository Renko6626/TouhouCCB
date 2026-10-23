"""Audience rounds and paid predictions; money uses the existing user ledger."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import CheckConstraint, Column, DateTime, JSON, Numeric, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow():
    return datetime.now(timezone.utc)


class ReturnedRoundGuard(SQLModel, table=True):
    """Permanent cancellation intents, plus the shared lifecycle serialization row."""
    __tablename__ = 'wavelength_round_guard'
    round_key: str = Field(max_length=128, primary_key=True)
    cancelled: bool = Field(default=False)


class ReturnedRound(SQLModel, table=True):
    __tablename__ = 'wavelength_round'
    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed', 'settled', 'cancelled')", name='ck_wavelength_round_status'),
        CheckConstraint('target_position IS NULL OR target_position BETWEEN 0 AND 1000', name='ck_wavelength_target'),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    round_key: str = Field(max_length=128, unique=True)
    game_key: str = Field(max_length=128)
    round_number: int
    left_label: str = Field(max_length=200)
    right_label: str = Field(max_length=200)
    team_name: str = Field(default='', max_length=100)
    status: str = Field(default='open', max_length=16)
    rules: dict = Field(sa_column=Column(JSON, nullable=False))
    target_position: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    closed_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    finished_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))


class ReturnedPrediction(SQLModel, table=True):
    __tablename__ = 'wavelength_prediction'
    __table_args__ = (
        UniqueConstraint('round_id', 'user_id', name='uq_wavelength_prediction_round_user'),
        CheckConstraint('position BETWEEN 0 AND 1000', name='ck_wavelength_prediction_position'),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    round_id: int = Field(foreign_key='wavelength_round.id')
    user_id: int = Field(foreign_key='user.id', index=True)
    position: int
    stake: Decimal = Field(sa_column=Column(Numeric(16, 6), nullable=False))
    distance: Optional[int] = None
    payout: Optional[Decimal] = Field(default=None, sa_column=Column(Numeric(16, 6)))
    submitted_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
