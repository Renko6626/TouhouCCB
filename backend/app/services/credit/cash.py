"""Cash purpose boundaries. Callers own User locking and write authorization.

Every short write also locks User before changing its lock, so reading persisted
short locks after the User lock serializes against all cooperating cash writers.
These read helpers neither commit nor acquire pair/gates after a User lock.
"""
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import User
from app.models.fx import FxShortPosition

ZERO = Decimal("0")


class CashInvariantError(ValueError):
    """Stored lock amounts and cash cannot form a valid account balance."""


async def restricted_cash(session: AsyncSession, user_id: int) -> Decimal:
    amount = (await session.execute(select(func.coalesce(
        func.sum(FxShortPosition.restricted_gold), ZERO,
    )).where(FxShortPosition.user_id == user_id))).scalar_one()
    value = Decimal(amount)
    if not value.is_finite() or value < ZERO:
        raise CashInvariantError("invalid restricted cash balance")
    return value


async def available_cash(session: AsyncSession, user: User, *, locked: Decimal | None = None) -> Decimal:
    cash = Decimal(user.cash)
    if locked is None:
        locked = await restricted_cash(session, user.id)
    if not locked.is_finite() or locked < ZERO:
        raise CashInvariantError("invalid restricted cash balance")
    if not cash.is_finite() or cash < locked:
        raise CashInvariantError(f"restricted cash exceeds total cash: cash={cash}, restricted={locked}")
    return cash - locked


async def has_foreign_debt(session: AsyncSession, user_id: int) -> bool:
    """Indexed by user_id; both principal and accrued interest are obligations."""
    row = (await session.execute(select(FxShortPosition.id).where(
        FxShortPosition.user_id == user_id,
        (FxShortPosition.principal_foreign > ZERO) | (FxShortPosition.interest_foreign > ZERO),
    ).limit(1))).first()
    return row is not None
