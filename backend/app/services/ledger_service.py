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
from app.services import audit_service

# ledger entry_type → audit event_type（同名）
_AUDIT_TYPE = {
    "borrow": "loan_borrow",
    "repay": "loan_repay",
    "admin_adjust_cash": "admin_adjust_cash",
    "admin_force_loan": "admin_force_loan",
    "admin_forgive_debt": "admin_forgive_debt",
    "admin_amnesty": "admin_amnesty",
}


async def record_entry(
    session: AsyncSession,
    *,
    user: User,
    entry_type: str,
    cash_delta: Decimal,
    debt_delta: Decimal,
    daily_rate: Optional[Decimal],
    operator_user_id: Optional[int] = None,
    reason: Optional[str] = None,
    interest_accrued: Decimal = Decimal("0"),
) -> LedgerEntry:
    """构造并 add 一条 LedgerEntry（flush 取 id），并追加一条同事务的 audit_event。不 commit（调用方负责）。

    快照（cash_after/debt_after/debt_last_accrued_at_after）从 user 对象当前值读，
    因此必须在 user 资金已变动之后调用。

    interest_accrued：本次操作前隐式结算进 debt 的利息（借/还路径会先 accrue），
    仅进审计事件 payload，便于重放时区分「利息」与「本金变动」。
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
    await session.flush()   # 拿 entry.id 作审计事件的 ref_id
    audit_service.record(
        session, _AUDIT_TYPE[entry_type],
        user_id=user.id,
        operator_user_id=operator_user_id,
        ref_table="ledger_entry", ref_id=entry.id,
        payload={
            "cash_delta": cash_delta,
            "debt_delta": debt_delta,
            "daily_rate": daily_rate,
            "interest_accrued": interest_accrued,
            "reason": reason,
        },
        user_after=audit_service.user_snapshot(user),
    )
    return entry
