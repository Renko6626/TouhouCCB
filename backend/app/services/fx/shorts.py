"""Pure foreign-debt interest primitives; callers own locks, audit and versions.

Opening is deliberately absent from this foundation. Before adding principal,
settle old debt and establish the new borrowing timestamp explicitly, including
when settlement produces only dust and leaves its timestamp unchanged.
"""
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from app.models.fx import FxShortPosition
from app.services.loan_service import pending_debt

_MAX_DEBT = Decimal('999999999999999999.999999')


def pending_short_debt(position: FxShortPosition, daily_rate: Decimal, now: datetime) -> Decimal:
    """Read with the same UTC, compound rate and six-place semantics as gold."""
    total = position.principal_foreign + position.interest_foreign
    result = pending_debt(SimpleNamespace(debt=total, debt_last_accrued_at=position.interest_last_accrued_at), daily_rate, now)
    if not result.is_finite() or result < 0 or result > _MAX_DEBT:
        raise ValueError('foreign debt exceeds storage range')
    return result


def accrue_short_interest(position: FxShortPosition, daily_rate: Decimal, now: datetime) -> Decimal:
    """Accrue only foreign interest, preserving principal and dust timestamps."""
    added = pending_short_debt(position, daily_rate, now) - position.principal_foreign - position.interest_foreign
    if added:
        position.interest_foreign += added
        position.interest_last_accrued_at = now
    return added
