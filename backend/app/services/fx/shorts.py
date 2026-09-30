"""Foreign-debt primitives and the atomic short-opening ledger.

Transaction contract (spec §7.1 / §11):

- ``pending_short_debt`` / ``accrue_short_interest`` are pure debt primitives.
- ``execute_short_open_in_session`` performs every validation, lock and balance
  mutation for opening or adding to a short **inside the caller's transaction**.
  It never commits, never rolls back and never publishes, so one outer caller
  can hold the complete GATE set, run the whole borrow/sell/risk bundle and
  commit once.  The caller owns lock order and gates; a stale dependency set or
  a concurrently changed price raises :class:`ShortRetryCredit` so the caller
  releases every lock and re-discovers rather than taking a missing pair GATE
  while holding the User lock (spec §11).

Lock order is ``pair -> User -> wallet/short rows (pair_id asc) -> treasury``.
Opening deliberately borrows real treasury foreign, sells it through the same
AMM and locks the net gold proceeds; it never credits ``FxWallet`` and never
auto-repays gold debt.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import TYPE_CHECKING, Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fx import FxPair, FxShortPosition, FxTrade, FxTreasury, FxWallet
from app.services import audit_service, loan_service, site_config
from app.services.credit import flags as credit_flags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.version import bump_economic_version, economic_version_of
from app.services.fx.amm import quote_sell
from app.services.loan_service import _elapsed_seconds, pending_debt
from app.services.market_locks import lock_user

if TYPE_CHECKING:  # risk imports this module, so only type-check the cycle.
    from app.services.credit.risk import DependencySet

_MAX_DEBT = Decimal('999999999999999999.999999')
_MAX_GOLD = Decimal('9999999999.999999')
_Q6 = Decimal('0.000001')

#: ``FxTrade.purpose`` for the borrow-and-sell opening action (spec §10).
SHORT_OPEN_PURPOSE = "short_open"


class ShortOpenRejected(ValueError):
    """Malformed request or a quote the AMM cannot execute."""


class ShortRetryCredit(Exception):
    """Release every lock/gate, re-discover dependencies, then retry."""


@dataclass(frozen=True, slots=True)
class FxShortExecution:
    """Result of one in-session short open.

    Scalars (``pair_id`` / ``post_price`` / ``trade_id``) are materialized so a
    post-commit publisher still has them after the caller rolls back to release
    locks.  ``replay`` is True when an existing idempotent trade was returned
    and nothing was mutated by this call.
    """

    trade: FxTrade
    replay: bool
    pair_id: int
    post_price: Decimal
    trade_id: int


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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── validation helpers ──────────────────────────────────────────────────────

def _require_writes() -> None:
    flags = credit_flags.get_flags()
    if (flags.unified_credit_enabled or flags.read_only_instance
            or credit_flags.read_only_from_env() or OWNERSHIP.reason is not None):
        OWNERSHIP.require_writes()


def _as_decimal(value: object, name: str) -> Decimal:
    parsed = _finite_decimal(value, name)
    if -parsed.as_tuple().exponent > 6:
        raise ShortOpenRejected(f"{name} must have at most 6 fractional digits")
    return parsed


def _finite_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ShortOpenRejected(f"{name} must be decimal") from exc
    if not parsed.is_finite():
        raise ShortOpenRejected(f"{name} must be finite")
    return parsed


def _positive_six(value: object, name: str) -> Decimal:
    parsed = _as_decimal(value, name)
    if parsed <= 0:
        raise ShortOpenRejected(f"{name} must be positive")
    return parsed


def _nonnegative_six(value: object, name: str) -> Decimal:
    parsed = _as_decimal(value, name)
    if parsed < 0:
        raise ShortOpenRejected(f"{name} must be non-negative")
    return parsed


def _require_gold_bound(value: Decimal, name: str) -> None:
    if value < 0 or value > _MAX_GOLD:
        raise ShortOpenRejected(f"{name} exceeds storage range")


def _require_foreign_bound(value: Decimal, name: str) -> None:
    if value < 0 or value > _MAX_DEBT:
        raise ShortOpenRejected(f"{name} exceeds storage range")


# ── locked reads (pair -> User -> wallet/short rows -> treasury) ─────────────

async def _lock_pair(db: AsyncSession, pair_id: int) -> FxPair:
    pair = (await db.execute(
        select(FxPair).where(FxPair.id == pair_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalars().first()
    if pair is None:
        raise HTTPException(status_code=404, detail="FX pair not found")
    return pair


async def _lock_wallet(db: AsyncSession, user_id: int, pair_id: int) -> Optional[FxWallet]:
    return (await db.execute(
        select(FxWallet).where(FxWallet.user_id == user_id, FxWallet.pair_id == pair_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalars().first()


async def _lock_short_rows(db: AsyncSession, user_id: int) -> list[FxShortPosition]:
    return list((await db.execute(
        select(FxShortPosition).where(FxShortPosition.user_id == user_id)
        .order_by(FxShortPosition.pair_id.asc())
        .with_for_update().execution_options(populate_existing=True)
    )).scalars().all())


async def _lock_treasury(db: AsyncSession, pair_id: int) -> Optional[FxTreasury]:
    return (await db.execute(
        select(FxTreasury).where(FxTreasury.pair_id == pair_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalars().first()


async def _pair_principal_sum(db: AsyncSession, pair_id: int) -> Decimal:
    """Σ unreturned foreign principal for one pair (interest is not capped)."""
    value = (await db.execute(
        select(func.coalesce(func.sum(FxShortPosition.principal_foreign), Decimal("0")))
        .where(FxShortPosition.pair_id == pair_id)
    )).scalar_one()
    return Decimal(value)


def _short_snapshot(position: Optional[FxShortPosition]) -> dict:
    if position is None:
        return {
            "short_position_id": None,
            "principal_foreign": Decimal("0"),
            "interest_foreign": Decimal("0"),
            "interest_last_accrued_at": None,
            "restricted_gold": Decimal("0"),
            "proceeds_basis_gold": Decimal("0"),
        }
    return {
        "short_position_id": position.id,
        "principal_foreign": position.principal_foreign,
        "interest_foreign": position.interest_foreign,
        "interest_last_accrued_at": position.interest_last_accrued_at,
        "restricted_gold": position.restricted_gold,
        "proceeds_basis_gold": position.proceeds_basis_gold,
    }


async def execute_short_open_in_session(
    db: AsyncSession,
    *,
    user_id: int,
    pair_id: int,
    foreign_amount: Decimal,
    min_gold_out: Decimal,
    idempotency_key: str,
    credit_deps: "DependencySet",
) -> FxShortExecution:
    """Borrow real foreign, sell it into the AMM and lock the proceeds.

    The caller must hold the target pair's exclusive gate and every dependency
    shared gate, and owns the single transaction (commit/rollback/SSE).  This
    function validates every §7.1 precondition under the lock order
    ``pair -> User -> wallet/short rows -> treasury``, runs the shared
    post-sell ``check_new_risk`` and returns the committed-in-transaction trade
    plus the scalars a post-commit publisher needs.
    """
    _require_writes()
    if not credit_flags.get_flags().unified_credit_enabled:
        raise HTTPException(status_code=403, detail="unified credit is not enabled")
    target_key = GroupKey("fx", pair_id)
    if target_key not in GATES.held_keys_by_current_task():
        raise RuntimeError("short-open caller must hold the target pair gate through commit")

    amount = _positive_six(foreign_amount, "foreign_amount")
    min_out = _nonnegative_six(min_gold_out, "min_gold_out")
    if not idempotency_key or len(idempotency_key) > 128:
        raise ShortOpenRejected("idempotency_key is required")
    if idempotency_key.startswith("liq:"):
        raise ShortOpenRejected("idempotency_key prefix is reserved")

    pair = await _lock_pair(db, pair_id)
    user = await lock_user(db, user_id)

    # Idempotency identity includes purpose/pair/foreign Q/min gold.  A hit
    # returns the saved execution with no mutation; the caller may roll back to
    # release locks.  Same (user, key) across spot/short or changed parameters
    # is a conflict, never a second borrow (spec §11).
    old = (await db.execute(select(FxTrade).where(
        FxTrade.user_id == user_id, FxTrade.idempotency_key == idempotency_key,
    ))).scalars().first()
    if old is not None:
        if (old.purpose != SHORT_OPEN_PURPOSE or old.pair_id != pair_id
                or old.side != "sell"
                or Decimal(old.requested_foreign_amount or 0) != amount
                or Decimal(old.min_out or 0) != min_out):
            raise HTTPException(status_code=409, detail="idempotency key parameter mismatch")
        return FxShortExecution(
            trade=old, replay=True, pair_id=int(old.pair_id),
            post_price=Decimal(old.post_price), trade_id=int(old.id),
        )

    held = GATES.held_keys_by_current_task()
    if economic_version_of(user) != credit_deps.economic_version:
        raise ShortRetryCredit()
    if not set(credit_deps.groups).issubset(held):
        raise ShortRetryCredit()
    snapshot = credit_deps.snapshots.get(target_key)
    if snapshot is None:
        raise ShortRetryCredit()

    if not await site_config.get_bool_or(db, "fx_enabled", False):
        raise HTTPException(status_code=403, detail="FX trading is disabled")
    if not await site_config.get_bool_or(db, "loan_enabled", False):
        raise HTTPException(status_code=403, detail="loans are disabled")
    if not await site_config.get_bool_or(db, "fx_short_enabled", False):
        raise HTTPException(status_code=403, detail="FX short opening is disabled")
    if (str(pair.status or "").strip().lower() != "trading"
            or bool(pair.reduce_only) or bool(pair.archived)):
        raise HTTPException(status_code=403, detail="FX pair is not open for shorting")
    if user.is_bot:
        raise HTTPException(status_code=403, detail="bot accounts cannot trade FX")
    if user.tos_accepted_at is None:
        raise HTTPException(status_code=403, detail="TOS acceptance required")
    if bool(user.credit_frozen):
        raise HTTPException(status_code=403, detail="credit is frozen")

    wallet = await _lock_wallet(db, user_id, pair_id)
    if wallet is not None and Decimal(wallet.foreign_amount) > 0:
        raise HTTPException(
            status_code=400,
            detail="this pair has spot foreign; sell it before opening a short",
        )
    positions = await _lock_short_rows(db, user_id)
    treasury = await _lock_treasury(db, pair_id)

    gold_reserve = Decimal(pair.gold_reserve)
    foreign_reserve = Decimal(pair.foreign_reserve)
    try:
        q = quote_sell(amount, gold_reserve, foreign_reserve, Decimal(pair.sell_fee_rate))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ShortOpenRejected(str(exc)) from exc
    if q.output_amount < min_out:
        raise HTTPException(status_code=409, detail="quoted output is below min_gold_out")

    if treasury is None or Decimal(treasury.foreign_balance) < amount:
        raise HTTPException(status_code=400, detail="insufficient treasury foreign balance")
    outstanding = await _pair_principal_sum(db, pair_id)
    limit = Decimal(pair.short_lending_limit_foreign)
    if outstanding + amount > limit:
        raise HTTPException(status_code=400, detail="short lending limit exceeded")

    target = next((p for p in positions if int(p.pair_id) == pair_id), None)
    now = utcnow()
    daily_rate = _finite_decimal(credit_deps.daily_rate, "daily_rate")
    settled_total = (
        pending_short_debt(target, daily_rate, now) if target is not None else Decimal("0")
    )
    new_total = settled_total + amount

    _require_foreign_bound(new_total, "short debt")
    _require_gold_bound(q.output_amount, "gold proceeds")
    _require_gold_bound(Decimal(user.cash) + q.output_amount, "cash")
    if target is not None:
        _require_gold_bound(Decimal(target.restricted_gold) + q.output_amount, "restricted gold")
        _require_gold_bound(Decimal(target.proceeds_basis_gold) + q.output_amount, "proceeds basis")
    _require_gold_bound(q.post_gold_reserve, "pool gold")
    _require_gold_bound(q.post_foreign_reserve, "pool foreign")
    _require_gold_bound(
        Decimal(treasury.foreign_balance) - amount + q.fee_amount, "treasury foreign")

    thresholds = credit_flags.get_flags().thresholds
    if thresholds is None:
        raise HTTPException(status_code=503, detail="credit risk engine is unavailable")

    # Imported here because credit.risk imports this module at module load.
    from app.services.credit import risk as credit_risk
    decision = await credit_risk.check_new_risk(
        db, user=user, deps=credit_deps,
        post=credit_risk.PostTradeState(
            cash=Decimal(user.cash) + q.output_amount,
            debt=Decimal(user.debt),
            short_debt={pair_id: new_total},
            short_reserves={pair_id: (q.post_gold_reserve, q.post_foreign_reserve)},
            base_versions={target_key: snapshot.version},
        ),
        thresholds=thresholds, partial_pct=Decimal("1"), now=now,
    )
    if not decision.allowed:
        if decision.reason == credit_risk.REASON_VERSION_CONFLICT:
            raise ShortRetryCredit()
        raise HTTPException(status_code=400, detail=decision.reason)

    _require_writes()
    pre_gold, pre_foreign = gold_reserve, foreign_reserve
    pre_treasury_gold = Decimal(treasury.gold_balance)
    pre_treasury_foreign = Decimal(treasury.foreign_balance)
    treasury_before = {
        "gold_balance": pre_treasury_gold,
        "foreign_balance": pre_treasury_foreign,
        "daily_spend": treasury.daily_spend,
        "spend_date": treasury.spend_date,
        "updated_at": treasury.updated_at,
    }
    user_before = {
        "cash": Decimal(user.cash),
        "debt": Decimal(user.debt),
        "debt_last_accrued_at": user.debt_last_accrued_at,
    }
    short_before = _short_snapshot(target)

    # One UTC T: settle gold debt and every relevant foreign position before
    # adding principal, so the new Q never inherits an older accrual base.
    before_debt = Decimal(user.debt)
    loan_service.accrue_interest(user, daily_rate, now)
    if Decimal(user.debt) != before_debt:
        audit_service.record(
            db, "interest_accrual", user_id=user.id,
            payload={"debt_before": before_debt, "debt_after": Decimal(user.debt),
                     "interest": Decimal(user.debt) - before_debt,
                     "daily_rate": daily_rate, "source": "fx_short_open"},
            user_after=audit_service.user_snapshot(user),
        )

    for pos in positions:
        if Decimal(pos.principal_foreign) + Decimal(pos.interest_foreign) <= 0:
            continue
        clock_before = pos.interest_last_accrued_at
        added = accrue_short_interest(pos, daily_rate, now)
        if added:
            audit_service.record_fx_short_interest(
                db, user=user, position=pos, pair_id=int(pos.pair_id),
                interest=added, daily_rate=daily_rate,
                elapsed_sec=(_elapsed_seconds(clock_before, now) if clock_before else None),
                interest_last_accrued_at_before=clock_before,
                accrued_at=now, source="fx_short_open",
            )
        if target is not None and pos is target:
            # Force the base even when settlement was dust-only.
            pos.interest_last_accrued_at = now

    if target is None:
        target = FxShortPosition(
            user_id=user_id, pair_id=pair_id,
            principal_foreign=Decimal("0"), interest_foreign=Decimal("0"),
            interest_last_accrued_at=now, restricted_gold=Decimal("0"),
            proceeds_basis_gold=Decimal("0"),
        )
        db.add(target)

    # Borrow Q from real inventory, sell it into this AMM; the foreign fee
    # returns to treasury while the net input joins the pool.
    pair.gold_reserve = q.post_gold_reserve
    pair.foreign_reserve = q.post_foreign_reserve
    pair.pool_version = int(pair.pool_version) + 1
    pair.updated_at = now

    treasury.foreign_balance = pre_treasury_foreign - amount + q.fee_amount
    treasury.updated_at = now

    user.cash = (Decimal(user.cash) + q.output_amount).quantize(_Q6)
    target.principal_foreign = (Decimal(target.principal_foreign) + amount).quantize(_Q6)
    target.restricted_gold = (Decimal(target.restricted_gold) + q.output_amount).quantize(_Q6)
    target.proceeds_basis_gold = (Decimal(target.proceeds_basis_gold) + q.output_amount).quantize(_Q6)
    target.interest_last_accrued_at = now
    target.updated_at = now
    bump_economic_version(user)

    # 0 <= ΣS <= C: proceeds are a subset of cash, never extra cash.
    other_locks = sum(
        (Decimal(p.restricted_gold) for p in positions if int(p.pair_id) != pair_id),
        Decimal("0"),
    )
    if other_locks + Decimal(target.restricted_gold) > Decimal(user.cash):
        raise ShortOpenRejected("restricted gold exceeds total cash")

    # Match the ``Numeric(16,6)`` storage so a replay returns the exact value
    # the first execution reported.
    post_price = q.post_price.quantize(_Q6)
    trade = FxTrade(
        pair_id=pair_id, user_id=user_id, side="sell", purpose=SHORT_OPEN_PURPOSE,
        requested_foreign_amount=amount, input_amount=q.input_amount,
        output_amount=q.output_amount, min_out=min_out, fee_amount=q.fee_amount,
        pre_gold_reserve=pre_gold, pre_foreign_reserve=pre_foreign,
        post_gold_reserve=q.post_gold_reserve, post_foreign_reserve=q.post_foreign_reserve,
        post_price=post_price, source="player", idempotency_key=idempotency_key,
    )
    db.add(trade)
    await db.flush()
    audit_service.record_fx_short_open(
        db, trade=trade, user=user, pair=pair, position=target, treasury=treasury,
        treasury_before=treasury_before,
        user_before=user_before, short_before=short_before,
        pool_before={"gold": pre_gold, "foreign": pre_foreign},
    )
    return FxShortExecution(
        trade=trade, replay=False, pair_id=int(pair_id),
        post_price=post_price, trade_id=int(trade.id),
    )
