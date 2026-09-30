"""Foreign-debt primitives and the atomic short open/cover ledgers.

Transaction contract (spec §7.1–7.3 / §11):

- ``pending_short_debt`` / ``accrue_short_interest`` are pure debt primitives.
- ``execute_short_open_in_session`` performs every validation, lock and balance
  mutation for opening or adding to a short **inside the caller's transaction**.
  It never commits, never rolls back and never publishes, so one outer caller
  can hold the complete GATE set, run the whole borrow/sell/risk bundle and
  commit once.  The caller owns lock order and gates; a stale dependency set or
  a concurrently changed price raises :class:`ShortRetryCredit` so the caller
  releases every lock and re-discovers rather than taking a missing pair GATE
  while holding the User lock (spec §11).
- ``execute_short_cover_in_session`` is the risk-reducing twin: it settles the
  debt at one UTC T, buys exactly q foreign through the AMM, releases only this
  position's lock (plus free cash) and repays interest then principal.  It is
  allowed below margin, while frozen and with the opening/loan gates off.

Lock order is ``pair -> User -> wallet/short rows (pair_id asc) -> treasury``
for opening and ``pair -> User -> short rows (pair_id asc) -> treasury`` for
covering; cover never touches or creates the spot wallet.  Opening deliberately
borrows real treasury foreign, sells it through the same AMM and locks the net
gold proceeds; covering never credits ``FxWallet`` and never auto-repays gold
debt.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import TYPE_CHECKING, Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import User
from app.models.fx import FxPair, FxShortPosition, FxTrade, FxTreasury, FxWallet
from app.schemas.fx import FxShortTradeResponse
from app.services import audit_service, loan_service, site_config
from app.services.credit import flags as credit_flags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.version import bump_economic_version, economic_version_of
from app.services.fx.amm import FxQuoteMath, quote_buy, quote_buy_exact_out, quote_sell
from app.services.fx.quantize import amount_down
from app.services.loan_service import _elapsed_seconds, pending_debt
from app.services.market_locks import lock_user

if TYPE_CHECKING:  # risk imports this module, so only type-check the cycle.
    from app.services.credit.risk import DependencySet

_logger = logging.getLogger(__name__)

_MAX_DEBT = Decimal('999999999999999999.999999')
_MAX_GOLD = Decimal('9999999999.999999')
_Q6 = Decimal('0.000001')

#: ``FxTrade.purpose`` for the borrow-and-sell opening action (spec §10).
SHORT_OPEN_PURPOSE = "short_open"

#: ``FxTrade.purpose`` for the exact-output buy-back/repay cover action.
SHORT_COVER_PURPOSE = "short_cover"

#: ``FxTrade.source`` for forced-liquidation covers (WP4, spec §8/§10).
LIQUIDATION_SOURCE = "liquidation"


class ShortRejected(ValueError):
    """Common base for any malformed short request or unexecutable short quote.

    A route that must safely turn every bad short input into a 4xx can catch
    this base instead of enumerating the open/cover subclasses.
    """


class ShortOpenRejected(ShortRejected):
    """Malformed open request or a quote the AMM cannot execute."""


class ShortCoverRejected(ShortRejected):
    """Malformed cover request or a cover quote the AMM cannot execute."""


class ShortLiquidationRejected(ShortRejected):
    """A forced cover that is malformed or produces an unstorable post-state.

    Pre-mutation validation failures raise this so the orchestrating caller's
    rollback leaves money/pool/treasury/debt/lock/trade/audit untouched.
    """


class ShortRetryCredit(Exception):
    """Release every lock/gate, re-discover dependencies, then retry."""


class ShortPostSettlementMismatch(ShortRetryCredit):
    """A settled short tail disagreed with the pure preflight quantity.

    Only raised *after* :func:`_settle_interest_at_T` advanced a clock, so the
    caller must roll the whole transaction back; it is never a normal blocked
    return.  Subclassing :class:`ShortRetryCredit` lets an orchestrator that
    already releases locks and re-discovers on a stale version handle it the
    same way.
    """


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
    """Read with the same UTC, compound rate and six-place semantics as gold.

    Raises :class:`ShortRejected` (not a bare ``ValueError``) when the accrued
    obligation overflows ``Numeric(24,6)`` so a player write route maps it to a
    clear 4xx instead of a 500 or a silently truncated/zero debt.
    """
    total = position.principal_foreign + position.interest_foreign
    try:
        result = pending_debt(SimpleNamespace(debt=total, debt_last_accrued_at=position.interest_last_accrued_at), daily_rate, now)
    except (ArithmeticError, InvalidOperation) as exc:
        # Compound interest can overflow the Decimal context before the range
        # check below ever sees a value; that is still an unrepresentable debt.
        raise ShortRejected('foreign debt exceeds storage range') from exc
    if not result.is_finite() or result < 0 or result > _MAX_DEBT:
        raise ShortRejected('foreign debt exceeds storage range')
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


def _as_decimal(
    value: object, name: str, reject: type[ValueError] = ShortOpenRejected,
) -> Decimal:
    parsed = _finite_decimal(value, name, reject)
    if -parsed.as_tuple().exponent > 6:
        raise reject(f"{name} must have at most 6 fractional digits")
    return parsed


def _finite_decimal(
    value: object, name: str, reject: type[ValueError] = ShortOpenRejected,
) -> Decimal:
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise reject(f"{name} must be decimal") from exc
    if not parsed.is_finite():
        raise reject(f"{name} must be finite")
    return parsed


def _positive_six(
    value: object, name: str, reject: type[ValueError] = ShortOpenRejected,
) -> Decimal:
    parsed = _as_decimal(value, name, reject)
    if parsed <= 0:
        raise reject(f"{name} must be positive")
    return parsed


def _nonnegative_six(
    value: object, name: str, reject: type[ValueError] = ShortOpenRejected,
) -> Decimal:
    parsed = _as_decimal(value, name, reject)
    if parsed < 0:
        raise reject(f"{name} must be non-negative")
    return parsed


def _require_gold_bound(
    value: Decimal, name: str, reject: type[ValueError] = ShortOpenRejected,
) -> None:
    if not value.is_finite() or value < 0 or value > _MAX_GOLD:
        raise reject(f"{name} exceeds storage range")


def _require_foreign_bound(
    value: Decimal, name: str, reject: type[ValueError] = ShortOpenRejected,
) -> None:
    if not value.is_finite() or value < 0 or value > _MAX_DEBT:
        raise reject(f"{name} exceeds storage range")


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


# ── shared cover settlement (player + forced liquidation) ───────────────────

async def _settle_interest_at_T(
    db: AsyncSession,
    *,
    user: User,
    positions: list[FxShortPosition],
    daily_rate: Decimal,
    now: datetime,
    source: str,
) -> None:
    """Accrue gold debt and every own foreign position once at one UTC T.

    A dust-only increment leaves the position clock untouched (see
    :func:`accrue_short_interest`) and therefore emits no event; a nonzero one
    is audited with the exact same T that governs later compounding.
    """
    before_debt = Decimal(user.debt)
    loan_service.accrue_interest(user, daily_rate, now)
    if Decimal(user.debt) != before_debt:
        audit_service.record(
            db, "interest_accrual", user_id=user.id,
            payload={"debt_before": before_debt, "debt_after": Decimal(user.debt),
                     "interest": Decimal(user.debt) - before_debt,
                     "daily_rate": daily_rate, "source": source},
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
                accrued_at=now, source=source,
            )


@dataclass(frozen=True, slots=True)
class _CoverLedger:
    """Mutation result shared by the player and forced-liquidation covers."""

    trade: FxTrade
    post_price: Decimal
    release: Decimal
    allocated_basis: Decimal
    realized_pl: Decimal
    interest_paid: Decimal
    principal_paid: Decimal


async def _write_cover_ledger(
    db: AsyncSession,
    *,
    pair: FxPair,
    user: User,
    target: FxShortPosition,
    positions: list[FxShortPosition],
    treasury: FxTreasury,
    now: datetime,
    quote: FxQuoteMath,
    q: Decimal,
    x: Decimal,
    q_effective: Decimal,
    release: Decimal,
    full_cover: bool,
    limited_by_cash: bool,
    requested_foreign_amount: Optional[Decimal],
    cover_all: Optional[bool],
    max_gold_in: Decimal,
    idempotency_key: str,
    source: str,
    treasury_before: dict,
    user_before: dict,
    short_before: dict,
    pre_gold: Decimal,
    pre_foreign: Decimal,
    reject: type[ValueError],
) -> _CoverLedger:
    """Validate every post-state, then post one cover in the caller's txn.

    Both the player cover and the forced-liquidation cover call this so they
    move identical physical legs (pool gold/foreign, treasury fee and returned
    q, user cash, economic version), repay interest before principal, apportion
    the lock and the historical proceeds basis independently and emit one
    replayable ``fx_trade`` audit package.  Every accumulator is range-checked
    **before** a single row moves: a validation failure raises ``reject`` with
    the caller's transaction still untouched, so a rollback cannot leave a
    half-posted ledger.
    """
    cash = Decimal(user.cash)
    position_lock = Decimal(target.restricted_gold)
    basis_before = Decimal(target.proceeds_basis_gold)
    total_lock = sum((Decimal(p.restricted_gold) for p in positions), Decimal("0"))
    pre_treasury_gold = Decimal(treasury_before["gold_balance"])
    pre_treasury_foreign = Decimal(treasury_before["foreign_balance"])

    interest_before = Decimal(target.interest_foreign)
    principal_before = Decimal(target.principal_foreign)
    interest_paid = min(q, interest_before)
    principal_paid = (q - interest_paid).quantize(_Q6)
    if full_cover:
        allocated_basis = basis_before
    else:
        allocated_basis = amount_down(basis_before * q / q_effective)
    realized_pl = (allocated_basis - x).quantize(_Q6)
    post_lock = (position_lock - release).quantize(_Q6)
    post_basis = (basis_before - allocated_basis).quantize(_Q6)

    _require_gold_bound(pre_treasury_gold + quote.fee_amount, "treasury gold", reject)
    _require_gold_bound(pre_treasury_foreign + q, "treasury foreign", reject)
    _require_gold_bound(cash - x, "cash", reject)
    _require_gold_bound(quote.post_gold_reserve, "pool gold", reject)
    _require_gold_bound(quote.post_foreign_reserve, "pool foreign", reject)
    _require_gold_bound(post_lock, "restricted gold", reject)
    _require_gold_bound(post_basis, "proceeds basis", reject)
    if post_lock < 0 or post_basis < 0:
        raise reject("cover would leave a negative short tail")
    if total_lock - release < 0 or total_lock - release > cash - x:
        raise reject("restricted gold exceeds total cash")

    pair.gold_reserve = quote.post_gold_reserve
    pair.foreign_reserve = quote.post_foreign_reserve
    pair.pool_version = int(pair.pool_version) + 1
    pair.updated_at = now
    treasury.gold_balance = (pre_treasury_gold + quote.fee_amount).quantize(_Q6)
    treasury.foreign_balance = (pre_treasury_foreign + q).quantize(_Q6)
    treasury.updated_at = now
    user.cash = (cash - x).quantize(_Q6)
    bump_economic_version(user)

    # q repays interest first, then principal.
    target.interest_foreign = (interest_before - interest_paid).quantize(_Q6)
    target.principal_foreign = (principal_before - principal_paid).quantize(_Q6)
    target.restricted_gold = post_lock
    target.proceeds_basis_gold = post_basis
    if full_cover:
        target.principal_foreign = Decimal("0")
        target.interest_foreign = Decimal("0")
        target.restricted_gold = Decimal("0")
        target.proceeds_basis_gold = Decimal("0")
        target.interest_last_accrued_at = None
    target.updated_at = now

    post_price = quote.post_price.quantize(_Q6)
    trade = FxTrade(
        pair_id=int(pair.id), user_id=int(user.id), side="buy",
        purpose=SHORT_COVER_PURPOSE,
        requested_foreign_amount=requested_foreign_amount,
        cover_all=cover_all, max_gold_in=max_gold_in,
        input_amount=x, output_amount=q, min_out=Decimal("0"),
        fee_amount=quote.fee_amount,
        pre_gold_reserve=pre_gold, pre_foreign_reserve=pre_foreign,
        post_gold_reserve=quote.post_gold_reserve,
        post_foreign_reserve=quote.post_foreign_reserve,
        post_price=post_price, source=source, idempotency_key=idempotency_key,
    )
    db.add(trade)
    await db.flush()
    audit_service.record_fx_short_cover(
        db, trade=trade, user=user, pair=pair, position=target, treasury=treasury,
        treasury_before=treasury_before, user_before=user_before,
        short_before=short_before,
        pool_before={"gold": pre_gold, "foreign": pre_foreign},
        interest_paid_foreign=interest_paid, principal_paid_foreign=principal_paid,
        released_lock=release, allocated_proceeds_basis=allocated_basis,
        realized_pl=realized_pl, accrued_at=now, full_cover=full_cover,
        limited_by_cash=limited_by_cash,
    )
    return _CoverLedger(
        trade=trade, post_price=post_price, release=release,
        allocated_basis=allocated_basis, realized_pl=realized_pl,
        interest_paid=interest_paid, principal_paid=principal_paid,
    )


def _cover_identity_matches(
    old: FxTrade,
    *,
    pair_id: int,
    cover_all: bool,
    amount: Optional[Decimal],
    max_gold_in: Decimal,
) -> bool:
    """Whether a same-key trade is exactly this cover request (see spec §11).

    Fixed q and ``cover_all`` are distinct identities, and the purpose/pair/max
    gold cap are part of the key so a spot trade, a short open or a changed
    request can never replay this execution.
    """
    if (old.purpose != SHORT_COVER_PURPOSE or int(old.pair_id) != int(pair_id)
            or old.side != "buy" or old.source != "player"):
        return False
    if bool(old.cover_all) != cover_all:
        return False
    if cover_all:
        if old.requested_foreign_amount is not None:
            return False
    else:
        if old.requested_foreign_amount is None:
            return False
        if Decimal(old.requested_foreign_amount) != amount:
            return False
    if old.max_gold_in is None or Decimal(old.max_gold_in) != max_gold_in:
        return False
    return True


async def execute_short_cover_in_session(
    db: AsyncSession,
    *,
    user_id: int,
    pair_id: int,
    foreign_amount: Optional[Decimal],
    cover_all: bool,
    max_gold_in: Decimal,
    idempotency_key: str,
    credit_deps: "DependencySet",
) -> FxShortExecution:
    """Buy back exactly the requested (or entire) foreign debt and repay it.

    Risk-reducing, so it is allowed below the initial margin, while credit is
    frozen or with the opening/loan gates off (spec §9); only total
    ``fx_enabled=false`` stops user trading.  The caller must hold the target
    pair's exclusive gate and every dependency shared gate, and owns the single
    transaction.  Lock order for cover is
    ``pair -> User -> short rows (pair_id asc) -> treasury``; the spot wallet is
    deliberately never touched and never created.

    At one common UTC T the gold debt and every own foreign position are settled
    first; ``cover_all`` then locks the **post-settlement** ``principal+interest``
    so a new interest leg cannot leave a dust remainder.  The exact-output quote
    is always re-run from the locked reserves, and the gold cost is allocated
    against ``F_cash + S_position`` (never another short's lock).  This function
    never commits, never rolls back and never publishes.
    """
    _require_writes()
    if not credit_flags.get_flags().unified_credit_enabled:
        raise HTTPException(status_code=403, detail="unified credit is not enabled")
    target_key = GroupKey("fx", pair_id)
    if target_key not in GATES.held_keys_by_current_task():
        raise RuntimeError("short-cover caller must hold the target pair gate through commit")

    is_cover_all = bool(cover_all)
    if is_cover_all and foreign_amount is not None:
        raise ShortCoverRejected("cover_all and foreign_amount are mutually exclusive")
    if not is_cover_all and foreign_amount is None:
        raise ShortCoverRejected("foreign_amount is required unless cover_all")
    amount = (
        None if is_cover_all
        else _positive_six(foreign_amount, "foreign_amount", ShortCoverRejected)
    )
    limit_gold = _nonnegative_six(max_gold_in, "max_gold_in", ShortCoverRejected)
    _require_gold_bound(limit_gold, "max_gold_in", ShortCoverRejected)
    if not idempotency_key or len(idempotency_key) > 128:
        raise ShortCoverRejected("idempotency_key is required")
    if idempotency_key.startswith("liq:"):
        raise ShortCoverRejected("idempotency_key prefix is reserved")

    pair = await _lock_pair(db, pair_id)
    user = await lock_user(db, user_id)

    # A replay after the position is fully closed returns the saved execution
    # and never buys foreign again.  Purpose/pair/cover_all/q/max identity must
    # all match; a spot trade or changed request on the same key is a conflict.
    old = (await db.execute(select(FxTrade).where(
        FxTrade.user_id == user_id, FxTrade.idempotency_key == idempotency_key,
    ))).scalars().first()
    if old is not None:
        if not _cover_identity_matches(
            old, pair_id=pair_id, cover_all=is_cover_all,
            amount=amount, max_gold_in=limit_gold,
        ):
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
    if credit_deps.snapshots.get(target_key) is None:
        raise ShortRetryCredit()

    if not await site_config.get_bool_or(db, "fx_enabled", False):
        raise HTTPException(status_code=403, detail="FX trading is disabled")
    status = str(pair.status or "").strip().lower()
    coverable = (
        not bool(pair.archived)
        and (status == "trading" or (status == "paused" and bool(pair.reduce_only)))
    )
    if not coverable:
        # paused without reduce_only, closed/draft/archived: block the user
        # action but retain the debt and locks (spec §9).
        raise HTTPException(status_code=403, detail="FX pair is not coverable")

    positions = await _lock_short_rows(db, user_id)
    target = next((p for p in positions if int(p.pair_id) == pair_id), None)
    if target is None or (
        Decimal(target.principal_foreign) + Decimal(target.interest_foreign) <= 0
    ):
        # Never turn an absent short into a spot buy or create a positive wallet.
        raise HTTPException(status_code=400, detail="no outstanding short to cover")
    treasury = await _lock_treasury(db, pair_id)
    if treasury is None:
        treasury = FxTreasury(pair_id=pair_id)
        db.add(treasury)

    now = utcnow()
    daily_rate = _finite_decimal(
        credit_deps.daily_rate, "daily_rate", ShortCoverRejected)

    # One UTC T: settle gold debt and every own foreign position before reading
    # the coverable debt, so cover_all takes the whole post-settlement tail.
    await _settle_interest_at_T(
        db, user=user, positions=positions, daily_rate=daily_rate,
        now=now, source="fx_short_cover")

    q_effective = (
        Decimal(target.principal_foreign) + Decimal(target.interest_foreign)
    ).quantize(_Q6)
    if q_effective <= 0:
        raise HTTPException(status_code=400, detail="no outstanding short to cover")
    q = q_effective if is_cover_all else amount
    if q > q_effective:
        raise HTTPException(status_code=400, detail="foreign_amount exceeds outstanding short")
    full_cover = q == q_effective

    gold_reserve = Decimal(pair.gold_reserve)
    foreign_reserve = Decimal(pair.foreign_reserve)
    try:
        quote = quote_buy_exact_out(
            q, gold_reserve, foreign_reserve, Decimal(pair.buy_fee_rate))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ShortCoverRejected(str(exc)) from exc
    x = quote.input_amount
    if x > limit_gold:
        raise HTTPException(status_code=409, detail="quoted gold cost exceeds max_gold_in")

    # Cash purpose boundary: this cover may spend only F_cash + its own lock.
    cash = Decimal(user.cash)
    position_lock = Decimal(target.restricted_gold)
    total_lock = sum((Decimal(p.restricted_gold) for p in positions), Decimal("0"))
    if cash < total_lock:
        raise ShortCoverRejected("restricted gold exceeds total cash")
    free_cash = cash - total_lock
    base_release = (
        position_lock if full_cover else amount_down(position_lock * q / q_effective)
    )
    if x <= free_cash + base_release:
        release = base_release
    elif x <= free_cash + position_lock:
        # A losing cover may consume extra gold from this position's own lock,
        # never another short's, and never a new gold loan.
        release = max(base_release, x - free_cash)
    else:
        raise HTTPException(status_code=400, detail="insufficient cash to cover")
    release = release.quantize(_Q6)

    _require_writes()
    pre_gold, pre_foreign = gold_reserve, foreign_reserve
    treasury_before = {
        "gold_balance": Decimal(treasury.gold_balance),
        "foreign_balance": Decimal(treasury.foreign_balance),
        "daily_spend": treasury.daily_spend,
        "spend_date": treasury.spend_date,
        "updated_at": treasury.updated_at,
    }
    user_before = {
        "cash": cash,
        "debt": Decimal(user.debt),
        "debt_last_accrued_at": user.debt_last_accrued_at,
    }
    short_before = _short_snapshot(target)
    ledger = await _write_cover_ledger(
        db, pair=pair, user=user, target=target, positions=positions,
        treasury=treasury, now=now, quote=quote, q=q, x=x,
        q_effective=q_effective, release=release, full_cover=full_cover,
        limited_by_cash=False,
        requested_foreign_amount=(None if is_cover_all else q),
        cover_all=(True if is_cover_all else None), max_gold_in=limit_gold,
        idempotency_key=idempotency_key, source="player",
        treasury_before=treasury_before, user_before=user_before,
        short_before=short_before, pre_gold=pre_gold, pre_foreign=pre_foreign,
        reject=ShortCoverRejected,
    )
    return FxShortExecution(
        trade=ledger.trade, replay=False, pair_id=int(pair_id),
        post_price=ledger.post_price, trade_id=int(ledger.trade.id),
    )


#: Stable blocked reasons WP4c can persist on a forced-cover action (spec §8).
LIQ_COVER_BLOCKED_PAIR = "pair_not_coverable"
LIQ_COVER_BLOCKED_NO_SHORT = "no_outstanding_short"
LIQ_COVER_BLOCKED_POOL = "insufficient_pool_foreign"
LIQ_COVER_BLOCKED_RESERVE = "invalid_short_reserve"
LIQ_COVER_BLOCKED_CASH = "insufficient_cash"
LIQ_COVER_BLOCKED_QUOTE = "quote_failed"


@dataclass(frozen=True, slots=True)
class FxLiquidationCoverExecution:
    """Result of one budgeted forced cover inside the caller's transaction.

    Exactly one shape is returned:

    - **blocked**: ``blocked_reason`` is set, ``trade`` is None and no row was
      mutated; the caller may record the reason and commit the round.
    - **executed / replay**: ``trade`` is set.  ``replay`` is True when the
      ``(user_id, liq:{run_id}:{round_no})`` key already had a committed trade,
      so the caller must not debit/repay a second time nor create a second
      action; the post-state legs are re-read from the saved audit package.
    """

    trade: Optional[FxTrade]
    replay: bool
    blocked_reason: Optional[str]
    pair_id: int
    idempotency_key: str
    post_price: Optional[Decimal]
    trade_id: Optional[int]
    repaid_foreign: Decimal
    paid_gold: Decimal
    fee_gold: Decimal
    released_lock: Decimal
    limited_by_cash: bool
    full_cover: bool
    interest_paid_foreign: Decimal
    principal_paid_foreign: Decimal
    allocated_proceeds_basis: Decimal
    realized_pl: Decimal
    principal_foreign_after: Decimal
    interest_foreign_after: Decimal
    restricted_gold_after: Decimal
    proceeds_basis_gold_after: Decimal
    accrued_at: Optional[datetime]


def _liquidation_cover_identity_matches(
    old: FxTrade, *, pair_id: int, planned: Decimal, budget_cap: Decimal,
) -> bool:
    """Whether a same-key trade is exactly this forced-cover request.

    The raw request identity (planned quantity and gold budget) is part of the
    key, so a crash-retry with changed parameters, a prior forced *sell* on the
    same round, or a player trade can never replay this execution.
    """
    return (
        old.purpose == SHORT_COVER_PURPOSE
        and int(old.pair_id) == int(pair_id)
        and old.side == "buy"
        and old.source == LIQUIDATION_SOURCE
        and old.cover_all is None
        and old.requested_foreign_amount is not None
        and Decimal(old.requested_foreign_amount) == planned
        and old.max_gold_in is not None
        and Decimal(old.max_gold_in) == budget_cap
    )


def _liquidation_cover_blocked(
    *, pair_id: int, idempotency_key: str, reason: str,
) -> FxLiquidationCoverExecution:
    return FxLiquidationCoverExecution(
        trade=None, replay=False, blocked_reason=reason,
        pair_id=pair_id, idempotency_key=idempotency_key,
        post_price=None, trade_id=None,
        repaid_foreign=Decimal("0"), paid_gold=Decimal("0"),
        fee_gold=Decimal("0"), released_lock=Decimal("0"),
        limited_by_cash=False, full_cover=False,
        interest_paid_foreign=Decimal("0"), principal_paid_foreign=Decimal("0"),
        allocated_proceeds_basis=Decimal("0"), realized_pl=Decimal("0"),
        principal_foreign_after=Decimal("0"), interest_foreign_after=Decimal("0"),
        restricted_gold_after=Decimal("0"), proceeds_basis_gold_after=Decimal("0"),
        accrued_at=None,
    )


async def _replay_cover_metadata(db: AsyncSession, trade: FxTrade) -> dict:
    """Re-read the saved audit legs so a replay reports the original post-state."""
    from app.models.audit import AuditEvent

    event = (await db.execute(select(AuditEvent).where(
        AuditEvent.event_type == "fx_trade",
        AuditEvent.ref_table == "fx_trade",
        AuditEvent.ref_id == int(trade.id),
    ))).scalars().first()
    payload = {} if event is None else (event.payload or {})
    short_after = payload.get("short_after") or {}

    def _d(value) -> Decimal:
        return Decimal(str(value)) if value is not None else Decimal("0")

    accrued_raw = payload.get("accrued_at")
    try:
        accrued_at = datetime.fromisoformat(accrued_raw) if accrued_raw else None
    except (TypeError, ValueError):  # pragma: no cover - malformed legacy audit
        accrued_at = None
    return {
        "released_lock": _d(payload.get("released_lock")),
        "limited_by_cash": bool(payload.get("limited_by_cash")),
        "full_cover": bool(payload.get("full_cover")),
        "interest_paid_foreign": _d(payload.get("interest_paid_foreign")),
        "principal_paid_foreign": _d(payload.get("principal_paid_foreign")),
        "allocated_proceeds_basis": _d(payload.get("allocated_proceeds_basis")),
        "realized_pl": _d(payload.get("realized_pl")),
        "principal_foreign_after": _d(short_after.get("principal_foreign")),
        "interest_foreign_after": _d(short_after.get("interest_foreign")),
        "restricted_gold_after": _d(short_after.get("restricted_gold")),
        "proceeds_basis_gold_after": _d(short_after.get("proceeds_basis_gold")),
        "accrued_at": accrued_at,
    }


async def execute_liquidation_cover_in_session(
    db: AsyncSession,
    *,
    user_id: int,
    pair_id: int,
    run_id: int,
    round_no: int,
    planned_amount: Decimal,
    max_gold_budget: Decimal,
    credit_deps: "DependencySet",
) -> FxLiquidationCoverExecution:
    """Buy back foreign debt with a hard gold budget inside the caller's txn.

    Forced-liquidation twin of :func:`execute_short_cover_in_session` (spec
    §7.3/§8.1–8.2).  The caller (WP4c) already holds the target pair's
    exclusive GATE plus every dependency shared GATE and owns the single
    transaction; this function never commits, rolls back, publishes or creates a
    ``LiquidationAction``.

    - All player gates except the process-global ``unified_credit_enabled``
      fail-closed check are deliberately ignored: ``fx_enabled``, the loan gate,
      the short-opening gate and the credit freeze never block a forced cover.
      The real pair coverability matrix still applies (``trading`` or
      ``paused`` + ``reduce_only``; ``closed``/``draft``/``archived``/paused
      non-reduce-only retain the debt but block the action).
    - One UTC T settles the gold debt and every own foreign position once; the
      requested plan is capped by the *post-settlement* actual debt.
    - The budget is ``min(max_gold_budget, free_cash + own_restricted_gold)``;
      another short's lock is never touched and no gold debt is originated.
    - If the exact plan is quotable within budget it buys exactly the plan;
      otherwise it falls back to an exact-input buy bounded by the budget whose
      output must be a positive amount not above the plan.  If that input could
      over-deliver, it switches back to exact-output.  A zero budget or an
      output that rounds to zero blocks without any write.
    - Idempotent per ``(user_id, liq:{run_id}:{round_no})``: a replay returns the
      saved trade and the audit post-state with no second debit/repayment; a
      parameter mismatch is a 409 conflict.

    Every normal blocked return is **mutation-free**: the post-T debt, quote and
    budget are preflighted with the pure :func:`pending_short_debt` at the common
    T, so no debt, interest clock, economic version, audit event, cash or
    reserve moves before the block is decided; WP4c can persist the blocked round
    and commit safely.  Only a truly executable cover advances the clocks, once
    at T.  Every accumulator is range-checked before a row moves.  Malformed
    inputs or unstorable post-states raise :class:`ShortLiquidationRejected`; a
    stale economic version or dependency set raises :class:`ShortRetryCredit` so
    the caller re-discovers instead of taking a missing pair GATE under the User
    lock.  If the settled tail ever disagreed with the preflighted quantity the
    kernel raises :class:`ShortPostSettlementMismatch` (a
    :class:`ShortRetryCredit`), which is only reachable after a mutation and
    therefore requires the caller to roll back.
    """
    _require_writes()
    if not credit_flags.get_flags().unified_credit_enabled:
        raise HTTPException(status_code=403, detail="unified credit is not enabled")
    target_key = GroupKey("fx", pair_id)
    if target_key not in GATES.held_keys_by_current_task():
        raise RuntimeError(
            "liquidation-cover caller must hold the target pair gate through commit")

    # Imported lazily: ``fx.trading`` imports ``credit.risk`` which imports this
    # module at load time, so a module-level import would be a cycle.
    from app.services.fx.trading import liquidation_idempotency_key

    key = liquidation_idempotency_key(run_id, round_no)
    planned = _positive_six(
        planned_amount, "planned_amount", ShortLiquidationRejected)
    _require_foreign_bound(planned, "planned_amount", ShortLiquidationRejected)
    budget_cap = _nonnegative_six(
        max_gold_budget, "max_gold_budget", ShortLiquidationRejected)
    _require_gold_bound(budget_cap, "max_gold_budget", ShortLiquidationRejected)

    pair = await _lock_pair(db, pair_id)
    user = await lock_user(db, user_id)

    old = (await db.execute(select(FxTrade).where(
        FxTrade.user_id == user_id, FxTrade.idempotency_key == key,
    ))).scalars().first()
    if old is not None:
        if not _liquidation_cover_identity_matches(
            old, pair_id=pair_id, planned=planned, budget_cap=budget_cap,
        ):
            raise HTTPException(
                status_code=409,
                detail="liquidation idempotency key parameter mismatch",
            )
        metadata = await _replay_cover_metadata(db, old)
        return FxLiquidationCoverExecution(
            trade=old, replay=True, blocked_reason=None,
            pair_id=int(old.pair_id), idempotency_key=key,
            post_price=Decimal(old.post_price), trade_id=int(old.id),
            repaid_foreign=Decimal(old.output_amount),
            paid_gold=Decimal(old.input_amount),
            fee_gold=Decimal(old.fee_amount),
            **metadata,
        )

    held = GATES.held_keys_by_current_task()
    if economic_version_of(user) != credit_deps.economic_version:
        raise ShortRetryCredit()
    if not set(credit_deps.groups).issubset(held):
        raise ShortRetryCredit()
    if credit_deps.snapshots.get(target_key) is None:
        raise ShortRetryCredit()

    status = str(pair.status or "").strip().lower()
    coverable = (
        not bool(pair.archived)
        and (status == "trading" or (status == "paused" and bool(pair.reduce_only)))
    )
    if not coverable:
        return _liquidation_cover_blocked(
            pair_id=pair_id, idempotency_key=key, reason=LIQ_COVER_BLOCKED_PAIR)

    positions = await _lock_short_rows(db, user_id)
    target = next((p for p in positions if int(p.pair_id) == pair_id), None)
    if target is None or (
        Decimal(target.principal_foreign) + Decimal(target.interest_foreign) <= 0
    ):
        return _liquidation_cover_blocked(
            pair_id=pair_id, idempotency_key=key, reason=LIQ_COVER_BLOCKED_NO_SHORT)

    treasury = await _lock_treasury(db, pair_id)
    pre_treasury_gold = (
        Decimal("0") if treasury is None else Decimal(treasury.gold_balance))
    pre_treasury_foreign = (
        Decimal("0") if treasury is None else Decimal(treasury.foreign_balance))

    # The cash budget depends only on cash and the locked gold, neither of which
    # interest accrual changes; an unpayable or invariant-breaking attempt is
    # rejected before advancing any clock so even the T settlement is untouched.
    cash = Decimal(user.cash)
    position_lock = Decimal(target.restricted_gold)
    total_lock = sum((Decimal(p.restricted_gold) for p in positions), Decimal("0"))
    if cash < total_lock:
        raise ShortLiquidationRejected("restricted gold exceeds total cash")
    free_cash = cash - total_lock
    budget = amount_down(min(budget_cap, free_cash + position_lock))
    if budget <= 0:
        return _liquidation_cover_blocked(
            pair_id=pair_id, idempotency_key=key, reason=LIQ_COVER_BLOCKED_CASH)

    now = utcnow()
    daily_rate = _finite_decimal(
        credit_deps.daily_rate, "daily_rate", ShortLiquidationRejected)

    # ── pure preflight: no clock is advanced until the cover is executable ──
    # ``pending_short_debt`` is the exact post-T tail ``_settle_interest_at_T``
    # will produce, so the q/quote/budget decision (and therefore every normal
    # blocked return below) happens before any debt, clock or audit row moves.
    # That keeps a blocked round committable by WP4c with zero economic change.
    try:
        q_effective = pending_short_debt(target, daily_rate, now).quantize(_Q6)
    except ShortRejected as exc:
        # An unrepresentable pending obligation is a data error: reject with no
        # write instead of settling and persisting a false q.
        raise ShortLiquidationRejected(str(exc)) from exc
    if q_effective <= 0:
        return _liquidation_cover_blocked(
            pair_id=pair_id, idempotency_key=key, reason=LIQ_COVER_BLOCKED_NO_SHORT)

    gold_reserve = Decimal(pair.gold_reserve)
    foreign_reserve = Decimal(pair.foreign_reserve)
    if (not gold_reserve.is_finite() or not foreign_reserve.is_finite()
            or gold_reserve <= 0 or foreign_reserve <= 0):
        return _liquidation_cover_blocked(
            pair_id=pair_id, idempotency_key=key, reason=LIQ_COVER_BLOCKED_RESERVE)
    if q_effective >= foreign_reserve:
        # Q>=F cannot be fully bought no matter how often we partially cover:
        # record the pool block instead of spending cash (spec §8.2).
        return _liquidation_cover_blocked(
            pair_id=pair_id, idempotency_key=key, reason=LIQ_COVER_BLOCKED_POOL)

    requested = amount_down(min(planned, q_effective))
    if requested <= 0:
        return _liquidation_cover_blocked(
            pair_id=pair_id, idempotency_key=key, reason=LIQ_COVER_BLOCKED_NO_SHORT)

    buy_fee_rate = Decimal(pair.buy_fee_rate)
    planned_quote: Optional[FxQuoteMath] = None
    try:
        planned_quote = quote_buy_exact_out(
            requested, gold_reserve, foreign_reserve, buy_fee_rate)
    except (TypeError, ValueError, ArithmeticError):
        planned_quote = None

    if planned_quote is not None and planned_quote.input_amount <= budget:
        quote = planned_quote
        q = requested
        limited_by_cash = False
    else:
        try:
            exact_in = quote_buy(budget, gold_reserve, foreign_reserve, buy_fee_rate)
        except (TypeError, ValueError, ArithmeticError):
            reason = (
                LIQ_COVER_BLOCKED_POOL if planned_quote is None
                else LIQ_COVER_BLOCKED_QUOTE
            )
            return _liquidation_cover_blocked(
                pair_id=pair_id, idempotency_key=key, reason=reason)
        if exact_in.output_amount > requested:
            # Exact-input over-delivers: switch back to buying exactly the plan.
            if planned_quote is None or planned_quote.input_amount > budget:
                return _liquidation_cover_blocked(
                    pair_id=pair_id, idempotency_key=key,
                    reason=LIQ_COVER_BLOCKED_QUOTE)
            quote = planned_quote
            q = requested
            limited_by_cash = False
        else:
            quote = exact_in
            q = exact_in.output_amount
            limited_by_cash = q < requested

    if q <= 0 or q > q_effective:
        return _liquidation_cover_blocked(
            pair_id=pair_id, idempotency_key=key, reason=LIQ_COVER_BLOCKED_QUOTE)
    x = quote.input_amount
    if x <= 0 or x > budget:
        return _liquidation_cover_blocked(
            pair_id=pair_id, idempotency_key=key, reason=LIQ_COVER_BLOCKED_CASH)

    full_cover = q == q_effective
    base_release = (
        position_lock if full_cover else amount_down(position_lock * q / q_effective)
    )
    if x <= free_cash + base_release:
        release = base_release
    elif x <= free_cash + position_lock:
        # A losing forced cover may spend extra gold from this position's own
        # lock (never another short's) but never originates a gold loan.
        release = max(base_release, x - free_cash)
    else:  # pragma: no cover - budget <= free_cash + own lock makes this unreachable
        return _liquidation_cover_blocked(
            pair_id=pair_id, idempotency_key=key, reason=LIQ_COVER_BLOCKED_CASH)
    release = release.quantize(_Q6)

    # ── truly executable: advance every clock once at the common T ──────────
    _require_writes()
    await _settle_interest_at_T(
        db, user=user, positions=positions, daily_rate=daily_rate,
        now=now, source="fx_short_liquidation")

    # The preflight and the settlement read the same pure ``pending_debt`` at
    # the same T, so the settled tail must equal the priced quantity.  A
    # disagreement means a clock moved during settlement: the caller must roll
    # back rather than treat this as a normal blocked round.
    settled_q_effective = (
        Decimal(target.principal_foreign) + Decimal(target.interest_foreign)
    ).quantize(_Q6)
    if settled_q_effective != q_effective or not (Decimal("0") < q <= settled_q_effective):
        raise ShortPostSettlementMismatch(
            "settled short debt disagrees with the preflighted cover quantity")

    if treasury is None:
        treasury = FxTreasury(pair_id=pair_id)
        db.add(treasury)
        pre_treasury_gold = Decimal("0")
        pre_treasury_foreign = Decimal("0")
    pre_gold, pre_foreign = gold_reserve, foreign_reserve
    treasury_before = {
        "gold_balance": pre_treasury_gold,
        "foreign_balance": pre_treasury_foreign,
        "daily_spend": treasury.daily_spend,
        "spend_date": treasury.spend_date,
        "updated_at": treasury.updated_at,
    }
    user_before = {
        "cash": cash,
        "debt": Decimal(user.debt),
        "debt_last_accrued_at": user.debt_last_accrued_at,
    }
    short_before = _short_snapshot(target)
    ledger = await _write_cover_ledger(
        db, pair=pair, user=user, target=target, positions=positions,
        treasury=treasury, now=now, quote=quote, q=q, x=x,
        q_effective=q_effective, release=release, full_cover=full_cover,
        limited_by_cash=limited_by_cash, requested_foreign_amount=planned,
        cover_all=None, max_gold_in=budget_cap,
        idempotency_key=key, source=LIQUIDATION_SOURCE,
        treasury_before=treasury_before, user_before=user_before,
        short_before=short_before, pre_gold=pre_gold, pre_foreign=pre_foreign,
        reject=ShortLiquidationRejected,
    )
    return FxLiquidationCoverExecution(
        trade=ledger.trade, replay=False, blocked_reason=None,
        pair_id=int(pair_id), idempotency_key=key,
        post_price=ledger.post_price, trade_id=int(ledger.trade.id),
        repaid_foreign=q, paid_gold=x, fee_gold=quote.fee_amount,
        released_lock=release, limited_by_cash=limited_by_cash,
        full_cover=full_cover, interest_paid_foreign=ledger.interest_paid,
        principal_paid_foreign=ledger.principal_paid,
        allocated_proceeds_basis=ledger.allocated_basis,
        realized_pl=ledger.realized_pl,
        principal_foreign_after=Decimal(target.principal_foreign),
        interest_foreign_after=Decimal(target.interest_foreign),
        restricted_gold_after=Decimal(target.restricted_gold),
        proceeds_basis_gold_after=Decimal(target.proceeds_basis_gold),
        accrued_at=now,
    )


# ── request-path wrappers (transaction boundary, gates, retry, publication) ──

def _is_fx_trade_idempotency_conflict(exc: IntegrityError) -> bool:
    """Whether ``exc`` is the known ``(user_id, idempotency_key)`` unique race.

    Two requests for the same key on different pairs can both miss the replay
    lookup (they hold different pair GATES) and one loses the
    ``uq_fx_trade_user_idempotency`` insert at flush/commit.  Only the Postgres
    constraint name and the SQLite column-pair message are matched, so unrelated
    numeric/CHECK/FK integrity failures keep their 500 rather than becoming a
    misleading 409.  Reads the DBAPI ``orig`` text only: the full SQLAlchemy
    message embeds bind parameters, and a crafted idempotency key must not be
    able to spoof a conflict.
    """
    orig = getattr(exc, "orig", None)
    text = "" if orig is None else str(orig)
    return (
        "uq_fx_trade_user_idempotency" in text
        or "fx_trade.user_id, fx_trade.idempotency_key" in text
    )


async def _rollback_quietly(db: AsyncSession) -> None:
    """Best-effort rollback that never masks the original failure."""
    try:
        await db.rollback()
    except Exception:  # pragma: no cover - rollback must not hide the root cause
        _logger.exception("FX short rollback failed")


def _short_response(execution: FxShortExecution) -> FxShortTradeResponse:
    """Materialize the action response before any replay rollback expires the row."""
    trade = execution.trade
    return FxShortTradeResponse(
        trade_id=int(trade.id),
        pair_id=int(trade.pair_id),
        purpose=str(trade.purpose),
        side=str(trade.side),
        requested_foreign_amount=(
            None if trade.requested_foreign_amount is None
            else Decimal(trade.requested_foreign_amount)
        ),
        cover_all=None if trade.cover_all is None else bool(trade.cover_all),
        input_amount=Decimal(trade.input_amount),
        output_amount=Decimal(trade.output_amount),
        fee_amount=Decimal(trade.fee_amount),
        min_out=Decimal(trade.min_out),
        max_gold_in=None if trade.max_gold_in is None else Decimal(trade.max_gold_in),
        post_price=Decimal(trade.post_price),
        replay=bool(execution.replay),
        created_at=trade.created_at,
    )


async def _execute_player_short_write(
    db: AsyncSession, *, user_id: int, pair_id: int, run_in_session,
) -> FxShortTradeResponse:
    """Shared player-write wrapper for short open and cover (spec §7 / §11).

    Discovers the complete :class:`DependencySet` before taking gates, releases
    the discovery transaction, then holds the target FX GATE exclusive plus every
    other dependency group shared in total order and runs the kernel inside one
    caller-owned transaction.  ``ShortRetryCredit`` rolls back, releases row
    locks/GATES, rediscovers and retries within ``credit_risk_retry_limit``; no
    pair GATE is ever acquired while holding the User lock because the kernel
    raises instead of taking one.  A same-key replay rolls back to release locks
    and returns the saved trade with no second borrow/buy/commit.  On a fresh
    success the transaction commits once and only then is the real
    ``FxTrade`` price frame handed to the bounded publisher.
    """
    _require_writes()
    # Imported lazily: credit.risk imports this module at module load.
    from app.services.credit.risk import discover_dependencies
    from app.services.fx import publisher

    target = GroupKey("fx", pair_id)
    retry_limit = max(0, int(credit_flags.get_flags().credit_risk_retry_limit))
    attempt = 0
    while True:
        deps = await discover_dependencies(db, user_id, extra_groups=[target])
        if db.in_transaction():
            await db.rollback()
        try:
            async with GATES.hold(
                exclusive=[target],
                shared=[group for group in deps.groups if group != target],
            ):
                try:
                    execution = await run_in_session(db, deps)
                    # Materialize before commit/replay-rollback: a rollback
                    # expires the live ORM row.
                    response = _short_response(execution)
                    if execution.replay:
                        await db.rollback()
                        return response
                    _require_writes()
                    await db.commit()
                except IntegrityError as exc:
                    await _rollback_quietly(db)
                    if not _is_fx_trade_idempotency_conflict(exc):
                        raise
                    # Expected same-key race: the winner committed elsewhere.
                    # All pending money/locks/audit are rolled back above; a
                    # same-key retry can read the saved trade (replay or a
                    # parameter-mismatch 409), never a second borrow.
                    raise HTTPException(
                        status_code=409,
                        detail="idempotency key conflict; retry",
                    ) from None
                except BaseException:
                    await _rollback_quietly(db)
                    raise
        except ShortRetryCredit:
            attempt += 1
            if attempt > retry_limit:
                raise HTTPException(
                    status_code=409, detail="version_conflict; retry") from None
            continue
        publisher.enqueue_publication(
            pair_id=execution.pair_id, post_price=execution.post_price,
            trade_id=execution.trade_id)
        return response


async def execute_short_open(
    db: AsyncSession, *, user_id: int, pair_id: int, foreign_amount: Decimal,
    min_gold_out: Decimal, idempotency_key: str,
) -> FxShortTradeResponse:
    """Request-path short open: owns gates, one commit, retry and publication."""
    async def run_in_session(session: AsyncSession, deps: "DependencySet") -> FxShortExecution:
        return await execute_short_open_in_session(
            session, user_id=user_id, pair_id=pair_id,
            foreign_amount=foreign_amount, min_gold_out=min_gold_out,
            idempotency_key=idempotency_key, credit_deps=deps,
        )

    return await _execute_player_short_write(
        db, user_id=user_id, pair_id=pair_id, run_in_session=run_in_session)


async def execute_short_cover(
    db: AsyncSession, *, user_id: int, pair_id: int,
    foreign_amount: Optional[Decimal], cover_all: bool, max_gold_in: Decimal,
    idempotency_key: str,
) -> FxShortTradeResponse:
    """Request-path short cover: risk-reducing, never routed through spot buy."""
    async def run_in_session(session: AsyncSession, deps: "DependencySet") -> FxShortExecution:
        return await execute_short_cover_in_session(
            session, user_id=user_id, pair_id=pair_id,
            foreign_amount=foreign_amount, cover_all=cover_all,
            max_gold_in=max_gold_in, idempotency_key=idempotency_key,
            credit_deps=deps,
        )

    return await _execute_player_short_write(
        db, user_id=user_id, pair_id=pair_id, run_in_session=run_in_session)


# ── read-only current position and indicative quote (Task 3c2) ───────────────

#: Advisory validity window; execution always re-quotes under lock and never
#: trusts this value (spec §10: the quote is a short-lived indication).
SHORT_QUOTE_TTL = timedelta(seconds=15)

#: ``RiskDecision`` reasons that mean "the risk engine short-circuited instead
#: of producing a numeric post-state"; their ``equity_after`` is not trustworthy.
_RISK_SHORT_CIRCUIT_REASONS = frozenset({
    "version_conflict", "credit_frozen", "frozen_by_operator",
})

#: Player-facing blocked reasons for the quote (stable strings, not i18n text).
BLOCKED_FX_DISABLED = "fx_disabled"
BLOCKED_UNIFIED_CREDIT = "unified_credit_disabled"
BLOCKED_LOANS_DISABLED = "loans_disabled"
BLOCKED_SHORT_DISABLED = "short_disabled"
BLOCKED_PAIR_NOT_OPEN = "pair_not_open"
BLOCKED_PAIR_NOT_COVERABLE = "pair_not_coverable"
BLOCKED_BOT_ACCOUNT = "bot_account"
BLOCKED_TOS_REQUIRED = "tos_required"
BLOCKED_CREDIT_FROZEN = "credit_frozen"
BLOCKED_SPOT_POSITION = "spot_position_exists"
BLOCKED_TREASURY_FOREIGN = "insufficient_treasury_foreign"
BLOCKED_LENDING_LIMIT = "short_lending_limit"
BLOCKED_NO_OUTSTANDING_SHORT = "no_outstanding_short"
BLOCKED_AMOUNT_EXCEEDS_DEBT = "foreign_amount_exceeds_outstanding"
BLOCKED_INSUFFICIENT_POOL = "insufficient_pool_foreign"
BLOCKED_INVALID_RESERVE = "invalid_short_reserve"
BLOCKED_INVALID_DEBT = "invalid_short_debt"
BLOCKED_QUOTE_FAILED = "quote_failed"
BLOCKED_INSUFFICIENT_CASH = "insufficient_cash"
BLOCKED_RESTRICTED_EXCEEDS_CASH = "restricted_cash_exceeds_cash"
BLOCKED_RISK_UNAVAILABLE = "risk_engine_unavailable"
BLOCKED_RISK_UNKNOWN = "short_quote_failed"


@dataclass(frozen=True, slots=True)
class FxShortPositionRead:
    """Materialized current-user short read (no ORM objects escape the session)."""

    pair_id: int
    currency_code: str
    principal_foreign: Decimal
    interest_foreign: Decimal
    pending_short_debt: Optional[Decimal]
    restricted_gold: Decimal
    proceeds_basis_gold: Decimal
    interest_last_accrued_at: Optional[datetime]
    reference_cover_cost: Optional[Decimal]
    reference_cover_fee: Optional[Decimal]
    executable: bool
    risk_status: str
    blocked_reason: Optional[str]


@dataclass(frozen=True, slots=True)
class FxShortQuoteRead:
    """Materialized indicative quote, safe to serialize after the session ends."""

    pair_id: int
    action: str
    purpose: str
    requested_foreign_amount: Optional[Decimal]
    cover_all: Optional[bool]
    actual_foreign_amount: Optional[Decimal]
    input_amount: Optional[Decimal]
    output_amount: Optional[Decimal]
    fee_amount: Optional[Decimal]
    fee_currency: str
    post_price: Optional[Decimal]
    pool_version: Optional[int]
    restricted_gold_delta: Optional[Decimal]
    available_cash: Optional[Decimal]
    affordable: Optional[bool]
    estimated_equity: Optional[Decimal]
    estimated_risk_basis: Optional[Decimal]
    risk_status: str
    risk_blocked_reason: Optional[str]
    executable: bool
    blocked_reason: Optional[str]
    expires_at: datetime


def _blocked_quote(
    *,
    pair_id: int,
    action: str,
    requested: Optional[Decimal],
    cover_all: Optional[bool],
    reason: str,
    expires_at: datetime,
    actual_foreign_amount: Optional[Decimal] = None,
    input_amount: Optional[Decimal] = None,
    output_amount: Optional[Decimal] = None,
    fee_amount: Optional[Decimal] = None,
    post_price: Optional[Decimal] = None,
    pool_version: Optional[int] = None,
    restricted_gold_delta: Optional[Decimal] = None,
    available_cash: Optional[Decimal] = None,
    affordable: Optional[bool] = None,
    estimated_equity: Optional[Decimal] = None,
    estimated_risk_basis: Optional[Decimal] = None,
    executable: bool = False,
    risk_reason: Optional[str] = None,
) -> FxShortQuoteRead:
    return FxShortQuoteRead(
        pair_id=pair_id,
        action=action,
        purpose="short_open" if action == "open" else "short_cover",
        requested_foreign_amount=requested,
        cover_all=cover_all,
        actual_foreign_amount=actual_foreign_amount,
        input_amount=input_amount,
        output_amount=output_amount,
        fee_amount=fee_amount,
        fee_currency="foreign" if action == "open" else "gold",
        post_price=post_price,
        pool_version=pool_version,
        restricted_gold_delta=restricted_gold_delta,
        available_cash=available_cash,
        affordable=affordable,
        estimated_equity=estimated_equity,
        estimated_risk_basis=estimated_risk_basis,
        risk_status="blocked",
        risk_blocked_reason=risk_reason,
        executable=executable,
        blocked_reason=reason,
        expires_at=expires_at,
    )


async def read_short_position(
    db: AsyncSession, *, user_id: int, pair_id: int,
) -> FxShortPositionRead:
    """Current user's row for one pair plus a reference whole-debt cover quote.

    Read-only: interest is computed with the pure :func:`pending_short_debt`, the
    stored clock is never advanced and nothing is committed.  A missing row is a
    zeroed position (like the wallet read), not a 404.  Treasury stock, the pair
    lending cap and other users' rows are never read or exposed.
    """
    from app.services.credit.fx_quote import FxShortPairSnapshot, quote_fx_short_group

    pair = (await db.execute(
        select(FxPair).where(FxPair.id == pair_id))).scalars().first()
    if pair is None:
        raise HTTPException(status_code=404, detail="FX pair not found")
    position = (await db.execute(select(FxShortPosition).where(
        FxShortPosition.user_id == user_id,
        FxShortPosition.pair_id == pair_id,
    ))).scalars().first()

    principal = Decimal(position.principal_foreign) if position is not None else Decimal("0")
    interest = Decimal(position.interest_foreign) if position is not None else Decimal("0")
    restricted = Decimal(position.restricted_gold) if position is not None else Decimal("0")
    basis = Decimal(position.proceeds_basis_gold) if position is not None else Decimal("0")
    accrued = position.interest_last_accrued_at if position is not None else None

    pending: Optional[Decimal]
    if position is None or principal + interest <= 0:
        pending = Decimal("0")
    else:
        try:
            daily_rate = await site_config.get_decimal_or(
                db, "loan_daily_rate", Decimal("0"))
            pending = pending_short_debt(position, daily_rate, utcnow())
        except ShortRejected:
            pending = None

    if pending is None:
        return FxShortPositionRead(
            pair_id=pair_id, currency_code=str(pair.currency_code),
            principal_foreign=principal, interest_foreign=interest,
            pending_short_debt=None, restricted_gold=restricted,
            proceeds_basis_gold=basis, interest_last_accrued_at=accrued,
            reference_cover_cost=None, reference_cover_fee=None,
            executable=False, risk_status="blocked",
            blocked_reason=BLOCKED_INVALID_DEBT,
        )

    snapshot = FxShortPairSnapshot(
        pair_id=pair_id,
        status=str(pair.status),
        reduce_only=bool(pair.reduce_only),
        gold_reserve=Decimal(pair.gold_reserve),
        foreign_reserve=Decimal(pair.foreign_reserve),
        buy_fee_rate=Decimal(pair.buy_fee_rate),
        sell_fee_rate=Decimal(pair.sell_fee_rate),
        pool_version=int(pair.pool_version),
    )
    quote = quote_fx_short_group(snapshot, foreign_debt=pending)
    if quote.gold_in is None:
        return FxShortPositionRead(
            pair_id=pair_id, currency_code=str(pair.currency_code),
            principal_foreign=principal, interest_foreign=interest,
            pending_short_debt=pending, restricted_gold=restricted,
            proceeds_basis_gold=basis, interest_last_accrued_at=accrued,
            reference_cover_cost=None, reference_cover_fee=None,
            executable=False, risk_status="blocked",
            blocked_reason=quote.blocked_reason or BLOCKED_RISK_UNKNOWN,
        )
    # Order eligibility is the intersection of the pair status, the total
    # player-trading stop and the process-global unified-credit gate, matching
    # the quote/write routes; the mathematical reference K and ``risk_status``
    # stay valid even when trading is stopped.
    if not await site_config.get_bool_or(db, "fx_enabled", False):
        order_reason: Optional[str] = BLOCKED_FX_DISABLED
    elif not credit_flags.get_flags().unified_credit_enabled:
        order_reason = BLOCKED_UNIFIED_CREDIT
    else:
        order_reason = quote.blocked_reason
    return FxShortPositionRead(
        pair_id=pair_id, currency_code=str(pair.currency_code),
        principal_foreign=principal, interest_foreign=interest,
        pending_short_debt=pending, restricted_gold=restricted,
        proceeds_basis_gold=basis, interest_last_accrued_at=accrued,
        reference_cover_cost=quote.gold_in, reference_cover_fee=quote.fee_gold,
        executable=order_reason is None, risk_status="ok",
        blocked_reason=order_reason,
    )


async def _quote_open_block(
    db: AsyncSession, *, pair: FxPair, user_id: int, amount: Decimal,
) -> Optional[str]:
    """First player gate that blocks opening/adding a short, if any (spec §9)."""
    status = str(pair.status or "").strip().lower()
    if not await site_config.get_bool_or(db, "loan_enabled", False):
        return BLOCKED_LOANS_DISABLED
    if not await site_config.get_bool_or(db, "fx_short_enabled", False):
        return BLOCKED_SHORT_DISABLED
    if status != "trading" or bool(pair.reduce_only) or bool(pair.archived):
        return BLOCKED_PAIR_NOT_OPEN
    user = (await db.execute(select(User).where(User.id == int(user_id)))).scalars().first()
    if user is None:
        return BLOCKED_RISK_UNAVAILABLE
    if bool(user.is_bot):
        return BLOCKED_BOT_ACCOUNT
    if user.tos_accepted_at is None:
        return BLOCKED_TOS_REQUIRED
    if bool(user.credit_frozen):
        return BLOCKED_CREDIT_FROZEN
    wallet = (await db.execute(select(FxWallet).where(
        FxWallet.user_id == user_id, FxWallet.pair_id == int(pair.id),
    ))).scalars().first()
    if wallet is not None and Decimal(wallet.foreign_amount) > 0:
        return BLOCKED_SPOT_POSITION
    treasury = (await db.execute(select(FxTreasury).where(
        FxTreasury.pair_id == int(pair.id)))).scalars().first()
    if treasury is None or Decimal(treasury.foreign_balance) < amount:
        return BLOCKED_TREASURY_FOREIGN
    outstanding = await _pair_principal_sum(db, int(pair.id))
    if outstanding + amount > Decimal(pair.short_lending_limit_foreign):
        return BLOCKED_LENDING_LIMIT
    return None


async def quote_short(
    db: AsyncSession,
    *,
    user_id: int,
    pair_id: int,
    action: str,
    foreign_amount: Optional[Decimal],
    cover_all: bool,
) -> FxShortQuoteRead:
    """Indicative open/cover quote; purely read-only (spec §5/§6/§9/§10).

    Order eligibility reuses the real AMM exact math; an open/adding follows the
    authoritative ``credit.risk.check_new_risk`` verdict, while a cover is
    reduce-only and stays executable even when admission fails.  The simulated
    ``estimated_equity`` / ``estimated_risk_basis`` come from
    ``credit.risk.value_post_state``: a **complete** post-order ``A/K/E/B``
    valuation that never takes the no-debt admission fast path, so uncached
    positive holdings and gold debt are always priced (spec §5.2).  Every value
    is advisory; the write routes re-quote and re-check min/max under lock.
    """
    from app.services.credit import risk as credit_risk

    now = utcnow()
    expires_at = now + SHORT_QUOTE_TTL
    normalized = str(action or "").strip().lower()
    if normalized not in ("open", "cover"):
        raise ShortOpenRejected("action must be open or cover")
    cover = normalized == "cover"
    is_cover_all = bool(cover_all) if cover else False
    reject = ShortCoverRejected if cover else ShortOpenRejected
    if cover:
        if is_cover_all and foreign_amount is not None:
            raise ShortCoverRejected("cover_all and foreign_amount are mutually exclusive")
        if not is_cover_all and foreign_amount is None:
            raise ShortCoverRejected("foreign_amount is required unless cover_all")
    else:
        if foreign_amount is None:
            raise ShortOpenRejected("foreign_amount is required for open")
        if is_cover_all:
            raise ShortOpenRejected("cover_all is not valid for open")
    amount: Optional[Decimal] = None
    if foreign_amount is not None:
        amount = _positive_six(foreign_amount, "foreign_amount", reject)
        _require_foreign_bound(amount, "foreign_amount", reject)

    pair = (await db.execute(
        select(FxPair).where(FxPair.id == pair_id))).scalars().first()
    if pair is None:
        raise HTTPException(status_code=404, detail="FX pair not found")

    def _blocked(reason, **kwargs) -> FxShortQuoteRead:
        return _blocked_quote(
            pair_id=pair_id, action=normalized,
            requested=(amount if not cover or not is_cover_all else None),
            cover_all=(is_cover_all if cover else None),
            reason=reason, expires_at=expires_at, **kwargs)

    if not await site_config.get_bool_or(db, "fx_enabled", False):
        return _blocked(BLOCKED_FX_DISABLED)
    if not credit_flags.get_flags().unified_credit_enabled:
        return _blocked(BLOCKED_UNIFIED_CREDIT)

    positions = list((await db.execute(select(FxShortPosition).where(
        FxShortPosition.user_id == int(user_id)))).scalars().all())
    from app.services.credit import cash as credit_cash
    try:
        total_lock = await credit_cash.restricted_cash(db, int(user_id))
    except credit_cash.CashInvariantError:
        return _blocked(BLOCKED_RESTRICTED_EXCEEDS_CASH)
    user_cash = Decimal((await db.execute(
        select(User.cash).where(User.id == int(user_id)))).scalar_one_or_none()
        or Decimal("0"))
    user_debt = Decimal((await db.execute(
        select(User.debt).where(User.id == int(user_id)))).scalar_one_or_none()
        or Decimal("0"))
    daily_rate = await site_config.get_decimal_or(db, "loan_daily_rate", Decimal("0"))
    free_cash = user_cash - total_lock
    available_cash = free_cash if free_cash >= 0 else None
    pool_version = int(pair.pool_version)

    if not cover:
        assert amount is not None
        blocked = await _quote_open_block(
            db, pair=pair, user_id=user_id, amount=amount)
        if blocked is not None:
            return _blocked(blocked, available_cash=available_cash,
                            pool_version=pool_version)
        gold_reserve = Decimal(pair.gold_reserve)
        foreign_reserve = Decimal(pair.foreign_reserve)
        try:
            sell = quote_sell(amount, gold_reserve, foreign_reserve,
                              Decimal(pair.sell_fee_rate))
        except (TypeError, ValueError, ArithmeticError):
            return _blocked(BLOCKED_QUOTE_FAILED, available_cash=available_cash,
                            pool_version=pool_version)
        target = next((p for p in positions if int(p.pair_id) == pair_id), None)
        if target is not None and (
                Decimal(target.principal_foreign) + Decimal(target.interest_foreign) > 0):
            try:
                settled = pending_short_debt(target, daily_rate, now)
            except ShortRejected:
                return _blocked(BLOCKED_INVALID_DEBT, available_cash=available_cash,
                                pool_version=pool_version)
        else:
            settled = Decimal("0")
        short_debt_after = settled + amount
        try:
            _require_foreign_bound(short_debt_after, "short debt")
        except ShortRejected:
            return _blocked(BLOCKED_INVALID_DEBT, available_cash=available_cash,
                            pool_version=pool_version)
        post_cash = user_cash + sell.output_amount
        post_gold = sell.post_gold_reserve
        post_foreign = sell.post_foreign_reserve
        restricted_delta = sell.output_amount
        quote_kwargs = dict(
            actual_foreign_amount=amount,
            input_amount=sell.input_amount,
            output_amount=sell.output_amount,
            fee_amount=sell.fee_amount,
            post_price=sell.post_price.quantize(_Q6),
            pool_version=pool_version,
            restricted_gold_delta=restricted_delta,
            available_cash=available_cash,
        )
    else:
        status = str(pair.status or "").strip().lower()
        coverable = (
            not bool(pair.archived)
            and (status == "trading" or (status == "paused" and bool(pair.reduce_only)))
        )
        target = next((p for p in positions if int(p.pair_id) == pair_id), None)
        if target is None or (
                Decimal(target.principal_foreign) + Decimal(target.interest_foreign) <= 0):
            return _blocked(BLOCKED_NO_OUTSTANDING_SHORT,
                            available_cash=available_cash, pool_version=pool_version)
        try:
            q_effective = pending_short_debt(target, daily_rate, now)
        except ShortRejected:
            return _blocked(BLOCKED_INVALID_DEBT,
                            available_cash=available_cash, pool_version=pool_version)
        if q_effective <= 0:
            return _blocked(BLOCKED_NO_OUTSTANDING_SHORT,
                            available_cash=available_cash, pool_version=pool_version)
        q = q_effective if is_cover_all else amount
        if q is None or q > q_effective:
            return _blocked(BLOCKED_AMOUNT_EXCEEDS_DEBT,
                            available_cash=available_cash, pool_version=pool_version)
        full_cover = q == q_effective
        gold_reserve = Decimal(pair.gold_reserve)
        foreign_reserve = Decimal(pair.foreign_reserve)
        if (not gold_reserve.is_finite() or not foreign_reserve.is_finite()
                or gold_reserve <= 0 or foreign_reserve <= 0):
            return _blocked(BLOCKED_INVALID_RESERVE, actual_foreign_amount=q,
                            available_cash=available_cash, pool_version=pool_version)
        if q >= foreign_reserve:
            return _blocked(BLOCKED_INSUFFICIENT_POOL, actual_foreign_amount=q,
                            available_cash=available_cash, pool_version=pool_version)
        try:
            buy = quote_buy_exact_out(
                q, gold_reserve, foreign_reserve, Decimal(pair.buy_fee_rate))
        except (TypeError, ValueError, ArithmeticError):
            return _blocked(BLOCKED_QUOTE_FAILED, actual_foreign_amount=q,
                            available_cash=available_cash, pool_version=pool_version)
        cover_order_reason = None if coverable else BLOCKED_PAIR_NOT_COVERABLE
        if user_cash < total_lock:
            return _blocked(BLOCKED_RESTRICTED_EXCEEDS_CASH, actual_foreign_amount=q,
                            available_cash=None, pool_version=pool_version)
        position_lock = Decimal(target.restricted_gold)
        base_release = (
            position_lock if full_cover
            else amount_down(position_lock * q / q_effective)
        )
        if buy.input_amount <= free_cash + base_release:
            release = base_release
            affordable = True
        elif buy.input_amount <= free_cash + position_lock:
            release = max(base_release, buy.input_amount - free_cash)
            affordable = True
        else:
            release = None
            affordable = False
            cover_order_reason = cover_order_reason or BLOCKED_INSUFFICIENT_CASH
        release = None if release is None else release.quantize(_Q6)
        post_cash = user_cash - buy.input_amount
        post_gold = buy.post_gold_reserve
        post_foreign = buy.post_foreign_reserve
        short_debt_after = (q_effective - q).quantize(_Q6)
        quote_kwargs = dict(
            actual_foreign_amount=q,
            input_amount=buy.input_amount,
            output_amount=q,
            fee_amount=buy.fee_amount,
            post_price=buy.post_price.quantize(_Q6),
            pool_version=pool_version,
            restricted_gold_delta=(None if release is None else -release),
            available_cash=available_cash,
            affordable=affordable,
        )
        if release is None:
            # Unaffordable: the AMM cost is real, but no valid post cash exists.
            return _blocked(cover_order_reason, **quote_kwargs)

    # ── shared-portfolio simulation via credit.risk (no duplication) ──
    def _result(
        *,
        estimated_equity: Optional[Decimal],
        estimated_basis: Optional[Decimal],
        risk_reason: Optional[str],
        order_reason: Optional[str],
    ) -> FxShortQuoteRead:
        """Split order eligibility from the portfolio-valuation signal.

        ``blocked_reason`` is populated only when an order-level gate blocks the
        action; ``risk_blocked_reason`` carries the risk/valuation reason even
        when the action stays executable.  A reduce-only cover is allowed while
        frozen, below margin or with another pair's K unknown (spec §5.2/§9), so
        its ``executable`` must not follow the simulated new-risk decision.
        """
        return FxShortQuoteRead(
            pair_id=pair_id,
            action=normalized,
            purpose="short_open" if not cover else "short_cover",
            requested_foreign_amount=(amount if (not cover or not is_cover_all) else None),
            cover_all=(is_cover_all if cover else None),
            actual_foreign_amount=quote_kwargs["actual_foreign_amount"],
            input_amount=quote_kwargs["input_amount"],
            output_amount=quote_kwargs["output_amount"],
            fee_amount=quote_kwargs["fee_amount"],
            fee_currency="foreign" if not cover else "gold",
            post_price=quote_kwargs["post_price"],
            pool_version=quote_kwargs["pool_version"],
            restricted_gold_delta=quote_kwargs["restricted_gold_delta"],
            available_cash=quote_kwargs["available_cash"],
            affordable=quote_kwargs.get("affordable"),
            estimated_equity=estimated_equity,
            estimated_risk_basis=estimated_basis,
            risk_status="ok" if estimated_equity is not None else "blocked",
            risk_blocked_reason=risk_reason,
            executable=order_reason is None,
            blocked_reason=order_reason,
            expires_at=expires_at,
        )

    def _risk_unavailable() -> FxShortQuoteRead:
        # No portfolio simulation: E/B stay null.  A cover's order eligibility is
        # independent of the risk engine (the write route never consults it), so
        # only an open is blocked by an unavailable simulation.
        return _result(
            estimated_equity=None, estimated_basis=None,
            risk_reason=BLOCKED_RISK_UNAVAILABLE,
            order_reason=cover_order_reason if cover else BLOCKED_RISK_UNAVAILABLE,
        )

    thresholds = credit_flags.get_flags().thresholds
    if thresholds is None:
        return _risk_unavailable()
    target_key = GroupKey("fx", pair_id)
    try:
        deps = await credit_risk.discover_dependencies(
            db, int(user_id), extra_groups=[target_key])
    except Exception:  # pragma: no cover - discovery read failure is fail-closed
        _logger.exception("FX short quote dependency discovery failed")
        return _risk_unavailable()
    snapshot = deps.snapshots.get(target_key)
    base_versions = {target_key: snapshot.version} if snapshot is not None else {}
    post = credit_risk.PostTradeState(
        cash=post_cash,
        debt=user_debt,
        short_debt={pair_id: short_debt_after},
        short_reserves={pair_id: (post_gold, post_foreign)},
        base_versions=base_versions,
    )
    fresh_user = (await db.execute(
        select(User).where(User.id == int(user_id)))).scalars().first()
    if fresh_user is None:
        return _risk_unavailable()
    try:
        decision = await credit_risk.check_new_risk(
            db, user=fresh_user, deps=deps, post=post, thresholds=thresholds,
            partial_pct=Decimal("1"), now=now,
        )
    except Exception:  # pragma: no cover - risk read failure is fail-closed
        _logger.exception("FX short quote risk simulation failed")
        return _risk_unavailable()

    if decision.reason in _RISK_SHORT_CIRCUIT_REASONS:
        # 风控短路（冻结/版本冲突）不是可信的完整净值：不报价，透出原因。
        estimated_equity = None
        estimated_basis = None
        risk_reason = decision.reason
    else:
        # 与放行判定解耦：完整交易后 A/K/E/B，不吃无债/无空头快路径。
        try:
            valuation = await credit_risk.value_post_state(
                db, user_id=int(user_id), deps=deps, post=post,
                thresholds=thresholds, now=now,
            )
        except Exception:  # pragma: no cover - valuation read failure is fail-closed
            _logger.exception("FX short quote post-state valuation failed")
            return _risk_unavailable()
        estimated_equity = valuation.equity
        estimated_basis = valuation.risk_basis
        risk_reason = valuation.blocked_reason

    if cover:
        order_reason = cover_order_reason
    elif decision.allowed:
        order_reason = None
    else:
        # Opening/adding is risk-increasing: a denied decision is an order block.
        order_reason = decision.reason or BLOCKED_RISK_UNKNOWN
    return _result(
        estimated_equity=estimated_equity, estimated_basis=estimated_basis,
        risk_reason=risk_reason, order_reason=order_reason,
    )
