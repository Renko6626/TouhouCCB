"""资金流水账本。

补齐借/还/管理员调账这些原本无历史记录的 cash/debt 变动。
利息不在此表（确定性可由公式 + 快照重算）；buy/sell/结算/强平/兑换/弹幕
各有自己的表，也不在此。详见 docs/superpowers/specs/2026-05-30-loan-ledger-design.md。
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, DateTime, Numeric, ForeignKey, Index


# 允许的 entry_type 取值（应用层收敛，不加 DB enum 约束，沿用 Transaction.type 惯例）
LEDGER_ENTRY_TYPES = frozenset({
    "borrow",              # 用户借款
    "repay",               # 用户还款
    "admin_adjust_cash",   # 管理员调现金
    "admin_force_loan",    # 管理员强制放贷
    "admin_forgive_debt",  # 管理员免债
})


class LedgerEntry(SQLModel, table=True):
    __tablename__ = "ledger_entry"
    __table_args__ = (
        Index("ix_ledger_entry_user_created", "user_id", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)

    entry_type: str = Field(max_length=32)

    # 本次变动（+/−，可为 0）
    cash_delta: Decimal = Field(sa_type=Numeric(16, 6))
    debt_delta: Decimal = Field(sa_type=Numeric(16, 6))

    # 操作后快照（重建锚点）
    cash_after: Decimal = Field(sa_type=Numeric(16, 6))
    debt_after: Decimal = Field(sa_type=Numeric(16, 6))

    # 利息重算锚点 + 当时利率
    debt_last_accrued_at_after: Optional[datetime] = Field(
        default=None, sa_type=DateTime(timezone=True),
    )
    daily_rate_at_event: Optional[Decimal] = Field(
        default=None, sa_type=Numeric(16, 8),
    )

    # 管理员操作时记操作者；用户自助为 None
    operator_user_id: Optional[int] = Field(
        default=None,
        sa_column=Column(ForeignKey("user.id"), nullable=True),
    )
    reason: Optional[str] = Field(default=None, max_length=200)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
        index=True,
    )
