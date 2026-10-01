"""Offline, atomic old-rate settlement. No live API calls this service."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import SiteConfig, User
from app.models.fx import FxShortPosition
from app.services import audit_service, site_config
from app.services.credit.ownership import LOCK_KEY, OwnershipError
from app.services.credit.version import bump_economic_version
from app.services.loan_service import pending_debt, _elapsed_seconds, _as_utc
from app.services.fx.shorts import pending_short_debt


class RateMaintenanceError(ValueError):
    pass


def _rate(value) -> Decimal:
    try:
        result = Decimal(value)
        if not result.is_finite() or not Decimal("0") < result < Decimal("1"):
            raise ValueError()
        return result
    except (ValueError, ArithmeticError, TypeError) as exc:
        raise RateMaintenanceError("daily rate must be finite and strictly between 0 and 1") from exc


def _amount(value: Decimal, maximum: Decimal) -> None:
    if not value.is_finite() or value < 0 or value > maximum:
        raise RateMaintenanceError("debt exceeds storage range")


def _clock(value, now):
    if value is None or _as_utc(value) > now:
        raise RateMaintenanceError("positive debt requires a valid clock no later than T")


async def _validate_schema(session):
    def check(conn):
        inspector = inspect(conn)
        # Capability check, not a hard-coded migration head: older schemas fail closed.
        for model in (User, FxShortPosition, SiteConfig):
            present = {column['name'] for column in inspector.get_columns(model.__table__.name)}
            required = {column.name for column in model.__table__.columns}
            if not required <= present:
                raise RateMaintenanceError(f"migrated schema required: {model.__table__.name}")
    await (await session.connection()).run_sync(check)


async def _change_rate_in_session(
    session: AsyncSession, *, new_rate: Decimal, operator_user_id: int | None,
    now: datetime, assert_offline_owner: Callable[[], None],
) -> dict:
    """Caller owns ONE transaction and offline ownership; injection is for tests only."""
    assert_offline_owner()
    if now.tzinfo is None:
        raise RateMaintenanceError("T must be timezone-aware UTC")
    now = now.astimezone(timezone.utc)
    new_rate = _rate(new_rate)
    await _validate_schema(session)
    config = (await session.execute(select(SiteConfig).where(
        SiteConfig.key == "loan_daily_rate").with_for_update())).scalar_one_or_none()
    if config is None or config.value_type != "decimal":
        raise RateMaintenanceError("persisted decimal loan_daily_rate required")
    old_rate = _rate(config.value)
    users = (await session.execute(select(User).order_by(User.id).with_for_update())).scalars().all()
    shorts = (await session.execute(select(FxShortPosition).order_by(
        FxShortPosition.user_id, FxShortPosition.pair_id).with_for_update())).scalars().all()
    by_user = {u.id: u for u in users}
    affected = set()
    gold_max = Decimal("9999999999.999999")
    foreign_max = Decimal("999999999999999999.999999")
    for user in users:
        _amount(user.debt, gold_max)
        if user.debt == 0:
            continue
        _clock(user.debt_last_accrued_at, now)
        before, clock = user.debt, user.debt_last_accrued_at
        try:
            after = pending_debt(user, old_rate, now)
        except ArithmeticError as exc:
            raise RateMaintenanceError("gold accrual overflow") from exc
        _amount(after, gold_max)
        user.debt, user.debt_last_accrued_at = after, now
        affected.add(user.id)
        audit_service.record(session, "interest_accrual", user_id=user.id, ts=now,
            payload={"debt_before": before, "debt_after": after, "interest": after-before,
                     "daily_rate": old_rate, "elapsed_sec": _elapsed_seconds(clock, now),
                     "debt_last_accrued_at_before": _as_utc(clock).isoformat(),
                     "debt_last_accrued_at_after": now.isoformat(), "accrued_at": now.isoformat(),
                     "source": "rate_maintenance"}, user_after=audit_service.user_snapshot(user))
    for position in shorts:
        _amount(position.principal_foreign, foreign_max)
        _amount(position.interest_foreign, foreign_max)
        total = position.principal_foreign + position.interest_foreign
        _amount(total, foreign_max)
        if total == 0:
            continue
        _clock(position.interest_last_accrued_at, now)
        clock = position.interest_last_accrued_at
        try:
            after = pending_short_debt(position, old_rate, now)
        except (ArithmeticError, ValueError) as exc:
            raise RateMaintenanceError("foreign accrual overflow") from exc
        _amount(after, foreign_max)
        delta = after-total
        position.interest_foreign += delta
        position.interest_last_accrued_at = now
        user = by_user[position.user_id]
        affected.add(user.id)
        audit_service.record_fx_short_interest(session, user=user, position=position,
            pair_id=position.pair_id, interest=delta, daily_rate=old_rate,
            elapsed_sec=_elapsed_seconds(clock, now), interest_last_accrued_at_before=clock,
            accrued_at=now, source="rate_maintenance")
    for uid in affected:
        bump_economic_version(by_user[uid])
    old = config.value
    config.value, config.updated_at, config.updated_by = str(new_rate), now, operator_user_id
    audit_service.record(session, "config_set", operator_user_id=operator_user_id, ts=now,
        payload={"key": "loan_daily_rate", "old": old, "new": str(new_rate),
                 "value_type": config.value_type, "accrued_at": now.isoformat(),
                 "source": "rate_maintenance"})
    assert_offline_owner()
    await session.flush()
    assert_offline_owner()
    return {"old_rate": old, "new_rate": str(new_rate), "T": now.isoformat(), "users": len(affected)}


async def change_loan_daily_rate(*, new_rate: Decimal, operator_user_id: int) -> dict:
    """Hold PostgreSQL ownership on the very connection that commits settlement.

    A transaction advisory lock conflicts with the application's session lock.
    Losing this connection loses both ownership and the uncommitted transaction;
    there is no independent heartbeat detection window.
    """
    from app.core.database import engine
    logger = logging.getLogger(__name__)
    connection = transaction = None
    try:
        if engine.dialect.name != "postgresql":
            raise OwnershipError("actual database engine must be PostgreSQL for offline rate maintenance")
        connection = await engine.connect()
        transaction = await connection.begin()
        acquired = bool((await connection.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOCK_KEY},
        )).scalar())
        if not acquired:
            raise OwnershipError("another economic writer owns the database advisory lock")

        def assert_transaction_owner():
            if not acquired or connection.closed or not transaction.is_active:
                raise OwnershipError("maintenance ownership transaction is no longer active")

        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            operator = await session.get(User, operator_user_id)
            if operator is None:
                raise RateMaintenanceError("operator user does not exist")
            result = await _change_rate_in_session(session, new_rate=new_rate,
                operator_user_id=operator_user_id, now=datetime.now(timezone.utc),
                assert_offline_owner=assert_transaction_owner)
        # Session cleanup occurs before the external transaction commits.
        assert_transaction_owner()
        await transaction.commit()
        try:
            site_config.clear_cache()
        except Exception as exc:
            logger.warning("rate maintenance committed; cache cleanup failed (%s)", type(exc).__name__)
        return result
    except BaseException:
        if transaction is not None and transaction.is_active:
            try:
                await transaction.rollback()
            except Exception as exc:
                logger.warning("rate maintenance rollback cleanup failed (%s)", type(exc).__name__)
        raise
    finally:
        if connection is not None:
            try:
                await connection.close()
            except Exception as exc:
                logger.warning("rate maintenance connection cleanup failed (%s)", type(exc).__name__)
        # Cleanup must not turn a successful commit into a CLI failure report.
        try:
            await engine.dispose()
        except Exception as exc:
            logger.warning("rate maintenance engine cleanup failed (%s)", type(exc).__name__)
