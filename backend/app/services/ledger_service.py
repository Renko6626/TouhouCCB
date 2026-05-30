"""LedgerEntry 写入助手。

调用方负责事务边界（与资金变动同事务）。快照字段从已变动的 user 对象读取，
所以调用前 user.cash/debt/debt_last_accrued_at 必须已是操作后的最终值。
"""
from __future__ import annotations
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import User
from app.models.ledger import LedgerEntry, LEDGER_ENTRY_TYPES


def record_entry(
    session: AsyncSession,
    *,
    user: User,
    entry_type: str,
    cash_delta: Decimal,
    debt_delta: Decimal,
    daily_rate: Optional[Decimal],
    operator_user_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> LedgerEntry:
    """构造并 add 一条 LedgerEntry。不 commit（调用方负责）。

    快照（cash_after/debt_after/debt_last_accrued_at_after）从 user 对象当前值读，
    因此必须在 user 资金已变动之后调用。
    """
    if entry_type not in LEDGER_ENTRY_TYPES:
        raise ValueError(f"unknown ledger entry_type: {entry_type}")
    entry = LedgerEntry(
        user_id=user.id,
        entry_type=entry_type,
        cash_delta=cash_delta,
        debt_delta=debt_delta,
        cash_after=user.cash,
        debt_after=user.debt,
        debt_last_accrued_at_after=user.debt_last_accrued_at,
        daily_rate_at_event=daily_rate,
        operator_user_id=operator_user_id,
        reason=reason,
    )
    session.add(entry)
    return entry
