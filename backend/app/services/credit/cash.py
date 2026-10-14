"""Cash balance and foreign-obligation helpers; callers own User locking."""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import User
from app.models.fx import FxShortPosition

ZERO = Decimal("0")


class CashInvariantError(ValueError):
    """Stored cash is not a valid non-negative balance."""


async def available_cash(session: AsyncSession, user: User) -> Decimal:
    """All cash is fungible; margin admission is checked separately by callers."""
    cash = Decimal(user.cash)
    if not cash.is_finite() or cash < ZERO:
        raise CashInvariantError("invalid cash balance")
    return cash


async def has_foreign_debt(session: AsyncSession, user_id: int) -> bool:
    """Indexed by user_id; both principal and accrued interest are obligations."""
    row = (await session.execute(select(FxShortPosition.id).where(
        FxShortPosition.user_id == user_id,
        (FxShortPosition.principal_foreign > ZERO) | (FxShortPosition.interest_foreign > ZERO),
    ).limit(1))).first()
    return row is not None
