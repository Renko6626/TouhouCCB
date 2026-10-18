"""Player FX quotes and atomic trades.

Transaction contract (WP4a/WP4b):

- ``quote`` / ``get_public_snapshot`` are pure reads: no writes, no commit.
- ``execute_trade_in_session`` performs every validation, lock and balance
  mutation for a player trade **inside the caller's transaction**.  It never
  commits, never rolls back and never publishes, so the same core can be
  reused by a caller that owns one larger transaction.
- ``execute_liquidation_sell_in_session`` is the forced-liquidation twin: it
  re-quotes the batch from the locked pair/wallet through the accepted
  ``services.credit.fx_quote`` interface, moves wallet/cash/pool/treasury in
  the same transaction and leaves debt repayment and the commit to the
  orchestration caller.  It is idempotent per ``(run_id, round_no)``.
- ``execute_trade`` releases the request transaction and submits a typed
  command to the pair writer. The writer owns commit, snapshot installation
  and post-commit hints, including idempotent replay rollback.

Lock order is always ``pair -> user -> wallet -> treasury`` in all three
execution paths.  Player order acceptance follows the F9 double-axis product
state (``trading`` / ``paused`` x ``reduce_only``); the full matrix is
documented on :func:`_player_side_allowed`.
"""
from __future__ import annotations
from app.services.market_read_state import FxReadState, stage_fx_pair

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import and_, bindparam, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.credit.cash import available_cash
from app.models.base import User
from app.services.fx.pool_events import append_pool_event
from app.models.fx import FxPair, FxShortPosition, FxTrade, FxTreasury, FxWallet
from app.schemas.fx import FxQuote, FxSnapshot, FxTradePublic, FxPairPublic
from app.services import audit_service, site_config, loan_service, ledger_service
from app.services.credit.fx_quote import FxGroupQuote, FxPairSnapshot, quote_fx_group
from app.services.fx import publisher
from app.services import trading_prices
from app.services.fx.amm import quote_buy, quote_sell, quote_buy_exact_out, amount_down, marginal_price
from app.services.fx.market_reads import fx_volume_24h, read_fx_snapshot_metadata
from app.services.market_locks import lock_user
from app.services.credit import flags as credit_flags
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.risk import DependencySet, PostTradeState, discover_dependencies, check_new_risk
from app.services.credit.version import bump_economic_version, economic_version_of

# SQL templates contain no balances or request-specific state.
_PAIR_READ = select(FxPair).where(FxPair.id == bindparam('fx_pair_id'))
_PAIR_LOCK = _PAIR_READ.with_for_update().execution_options(populate_existing=True)
_WALLET_LOCK = select(FxWallet).where(
    FxWallet.user_id == bindparam('fx_user_id'),
    FxWallet.pair_id == bindparam('fx_pair_id')
).with_for_update().execution_options(populate_existing=True)

_logger = logging.getLogger(__name__)

#: ``FxTrade.source`` for forced-liquidation sells (WP4 parent brief §WP4).
LIQUIDATION_SOURCE = "liquidation"


class _RetryCredit(Exception):
    """Release transaction and all gates before discovering dependencies again."""


def _require_writes():
    flags = credit_flags.get_flags()
    OWNERSHIP.require_writes()


class TradeRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FxTradeExecution:
    """Result of one in-session FX execution.

    ``trade`` is the live ORM row inside the caller's transaction.  ``public``
    is materialized before returning so it survives the wrapper's rollback on
    an idempotent replay (a rollback expires loaded ORM instances).  ``replay``
    is True when an existing idempotent trade was returned and nothing was
    mutated by this call.
    """

    trade: FxTrade
    public: FxTradePublic
    replay: bool


@dataclass(frozen=True, slots=True)
class FxLiquidationExecution:
    """Result of one in-session forced-liquidation sell.

    Exactly one shape is returned:

    - **blocked**: ``blocked_reason`` is set, ``trade`` is None and nothing was
      mutated (no wallet row is created, no reserve changes);
    - **executed / replay**: ``trade`` and ``public`` are set.  ``replay`` is
      True when the ``(run_id, round_no)`` idempotency key already had a
      committed trade, so the caller must not sell again or book a second
      action for that round.

    ``quote`` carries the fresh batch quote for executed/blocked attempts and
    is None on replay (the committed numbers are in ``public``/``trade``).
    """

    trade: Optional[FxTrade]
    public: Optional[FxTradePublic]
    quote: Optional[FxGroupQuote]
    replay: bool
    blocked_reason: Optional[str]
    idempotency_key: str


# Public snapshot prices are always quoted in gold per one foreign unit.
_PRICE_QUANT = Decimal("0.00000001")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _positive(value: Decimal, name: str) -> Decimal:
    try:
        value = Decimal(value)
    except Exception as exc:
        raise TradeRejected(f"{name} must be decimal") from exc
    if not value.is_finite() or value <= 0:
        raise TradeRejected(f"{name} must be finite and positive")
    if -value.as_tuple().exponent > 6:
        raise TradeRejected(f"{name} must have at most 6 fractional digits")
    return value


def _nonnegative(value: Decimal, name: str) -> Decimal:
    try:
        value = Decimal(value)
    except Exception as exc:
        raise TradeRejected(f"{name} must be decimal") from exc
    if not value.is_finite() or value < 0:
        raise TradeRejected(f"{name} must be finite and non-negative")
    if -value.as_tuple().exponent > 6:
        raise TradeRejected(f"{name} must have at most 6 fractional digits")
    return value


def _player_side_allowed(status: str, reduce_only: bool, side: str) -> bool:
    """F9 double-axis product state for **player** orders (spec §9.1).

    ================  ============  ==========================================
    status            reduce_only   player buy / sell
    ================  ============  ==========================================
    trading           false         buy allowed, sell allowed (legacy)
    trading           true          buy rejected, sell allowed (reduce-only)
    paused            true          buy rejected, sell allowed (operator opt-in)
    paused            false         fully halted (legacy semantics preserved)
    draft/closed/…    any           fully halted
    ================  ============  ==========================================

    ``paused`` therefore never becomes tradable implicitly: an operator must
    set ``reduce_only`` explicitly.  Forced liquidation uses the same product
    matrix through ``services.credit.fx_quote`` (see
    :func:`execute_liquidation_sell_in_session`).
    """
    normalized_status = str(status or "").strip().lower()
    normalized_side = str(side or "").strip().lower()
    if normalized_status == "trading":
        return normalized_side == "sell" or not reduce_only
    if normalized_status == "paused":
        return reduce_only and normalized_side == "sell"
    return False


async def _pair(db: AsyncSession, pair_id: int, *, lock: bool = False,
                fresh: bool = False) -> FxPair:
    statement = _PAIR_LOCK if lock else _PAIR_READ
    if fresh:
        statement = statement.execution_options(populate_existing=True)
    pair = (await db.execute(
        statement, {'fx_pair_id': pair_id}
    )).scalars().first()
    if pair is None:
        raise HTTPException(status_code=404, detail="FX pair not found")
    return pair


async def _read_pair_state(db: AsyncSession, pair_id: int) -> FxReadState:
    state = trading_prices.REGISTRY.peek_fx(pair_id)
    if state is not None:
        return state
    # Caller-owned transactions (including isolated test sessions) cannot wait
    # on a market GATE. Uncovered/read-only processes also retain that session.
    session = getattr(db, "sync_session", getattr(db, "_session", db))
    if trading_prices.REGISTRY._can_reuse("fx") and not session.in_transaction():
        return await trading_prices.REGISTRY.read_fx(pair_id)
    return FxReadState.from_pair(await _pair(db, pair_id))


async def read_public_pair_state(db: AsyncSession, pair_id: int) -> FxReadState:
    """Reuse memory only when the existing history row lock proves its version.

    A committed writer may still be installing memory. In that interval use
    this locked session's fresh row without installing it or acquiring GATES.
    """
    version = (await db.execute(select(FxPair.pool_version).where(
        FxPair.id == pair_id).with_for_update(read=True))).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="FX pair not found")
    state = trading_prices.REGISTRY.peek_fx(pair_id)
    if state is not None and state.pool_version == version:
        return state
    return FxReadState.from_pair(await _pair(db, pair_id, fresh=True))


def _math(pair: FxPair | FxReadState, side: str, amount: Decimal):
    side = str(side).lower()
    if side == "buy":
        return quote_buy(amount, pair.gold_reserve, pair.foreign_reserve, pair.buy_fee_rate)
    if side == "sell":
        return quote_sell(amount, pair.gold_reserve, pair.foreign_reserve, pair.sell_fee_rate)
    raise TradeRejected("side must be buy or sell")


def _gold_per_foreign(pair: FxPair | FxReadState, side: str) -> Decimal:
    """Effective quote normalized to **gold per foreign** (8dp).

    A player ``buy`` spends gold for foreign, so its effective price is
    ``input gold / output foreign`` (the ask).  A player ``sell`` gives foreign
    for gold, so its effective price is ``output gold / input foreign`` (the
    bid).  Both snapshot fields therefore share one unit and can be compared.
    """
    normalized = str(side).lower()
    q = _math(pair, normalized, Decimal("1"))
    if normalized == "buy":
        return (q.input_amount / q.output_amount).quantize(_PRICE_QUANT)
    return (q.output_amount / q.input_amount).quantize(_PRICE_QUANT)


async def quote(db: AsyncSession, pair_id: int, side: str, amount: Decimal,
                user_id: Optional[int] = None) -> FxQuote:
    amount = _positive(amount, "amount")
    pair = await _read_pair_state(db, pair_id)
    try:
        q = _math(pair, side, amount)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise TradeRejected(str(exc)) from exc
    return FxQuote(
        pair_id=pair.id, side=str(side).lower(), input_amount=q.input_amount,
        output_amount=q.output_amount, fee_amount=q.fee_amount,
        effective_price=q.effective_price, post_price=q.post_price,
        expires_at=utcnow(),
    )


async def _wallet_lock(db: AsyncSession, user_id: int, pair_id: int,
                       *, create: bool = True) -> Optional[FxWallet]:
    row = (await db.execute(
        _WALLET_LOCK, {'fx_user_id': user_id, 'fx_pair_id': pair_id}
    )).scalars().first()
    if row is None and create:
        row = FxWallet(user_id=user_id, pair_id=pair_id)
        db.add(row)
        await db.flush()
    return row


async def _rollback_quietly(db: AsyncSession) -> None:
    """Best-effort rollback that never masks the original failure."""
    try:
        await db.rollback()
    except Exception:
        _logger.exception("FX trade rollback failed")


def notify_market_data_committed(pair_id: int) -> None:
    """Best-effort synchronous post-commit hint to the FX market-data runtime.

    Must only be called after the caller's monetary transaction has committed.
    It marks the pair dirty and wakes the runtime loop; it performs no SQL,
    never awaits and never raises, so it can neither fail nor extend an
    already-committed trade.  A missed or duplicated hint is compensated by the
    runtime's periodic per-pair high-water reconciliation; correctness never
    depends on it.
    """
    try:
        from app.services.fx.market_state import FX_MARKET_DATA
        FX_MARKET_DATA.notify_committed(int(pair_id))
    except Exception:  # noqa: BLE001 - a hint must never affect the money path
        _logger.debug("fx market-data notify failed for pair %s", pair_id, exc_info=True)


async def _validate_bot(db, user, pair_id, profile_id, purpose, operator_id=None):
    """Internal spot admission; called after pair/user/wallet/treasury locks."""
    from app.models.bot import BotProfile
    from app.services.pve_fx.config import load_fx_config
    profile = (await db.execute(select(BotProfile).where(BotProfile.id == profile_id)
               .with_for_update().execution_options(populate_existing=True))).scalars().first()
    if (not user.is_active or not user.is_bot or profile is None
            or profile.user_id != user.id or profile.market_kind != 'fx'
            or profile.fx_pair_id != pair_id):
        raise HTTPException(status_code=403, detail='invalid FX bot binding')
    if purpose == 'scheduled':
        config = await load_fx_config(db)
        if (profile.status != 'active' or not config.enabled
                or pair_id not in config.pair_configs or not config.pair_configs[pair_id].enabled):
            raise HTTPException(status_code=403, detail='FX bot scheduling is disabled')
    elif purpose == 'admin_liquidate':
        operator = await db.get(User, operator_id) if operator_id is not None else None
        if profile.status != 'paused' or operator is None or not operator.is_active or not operator.is_superuser:
            raise HTTPException(status_code=403, detail='paused bot and superuser required')
    else:
        raise TradeRejected('invalid bot purpose')
    debt = (await db.execute(select(FxShortPosition.id).where(
        FxShortPosition.user_id == user.id,
        (FxShortPosition.principal_foreign > 0) | (FxShortPosition.interest_foreign > 0)))).first()
    if user.debt > 0 or debt is not None:
        raise HTTPException(status_code=403, detail='FX bot has outstanding debt')
    return profile


async def execute_trade_in_session(db: AsyncSession, user_id: int, pair_id: int, side: str,
                                   amount: Decimal, min_out: Decimal,
                                   idempotency_key: str, *,
                                   credit_deps: Optional[DependencySet] = None,
                                   borrow_amount: Optional[Decimal] = None,
                                   amount_mode: str = 'input',
                                   bot_profile_id: int | None = None,
                                   bot_purpose: str = 'scheduled',
                                   bot_operator_id: int | None = None,
                                   bot_reference_price: Decimal | None = None) -> FxTradeExecution:
    """Execute one player FX trade inside the caller's transaction.

    The target writer owns this transaction. Credit buys rediscover the complete
    own ledger after locking User and capture committed prices before simulation.
    Acquires product locks
    in ``pair -> user -> wallet -> treasury`` order and flushes its writes, but
    deliberately does **not** commit, roll back or publish.  On error the
    caller owns the transaction and must roll it back before reuse.
    """
    _require_writes()
    key = GroupKey("fx", pair_id)
    amount = _positive(amount, "amount")
    min_out = _nonnegative(min_out, "min_out")
    if not idempotency_key or len(idempotency_key) > 128:
        raise TradeRejected("idempotency_key is required")
    if idempotency_key.startswith("liq:"):
        raise TradeRejected("idempotency_key prefix is reserved")

    normalized = str(side).lower()
    if amount_mode not in ('input', 'foreign_out') or (amount_mode == 'foreign_out' and normalized != 'buy'):
        raise TradeRejected('invalid amount mode')
    if bot_profile_id is not None and borrow_amount is not None:
        raise TradeRejected('bot financing is forbidden')
    financed = borrow_amount is not None
    if financed:
        from app.services.fx.borrow_buy import validate_funding
        amount, borrow_amount = validate_funding(amount, borrow_amount)
        if normalized != 'buy':
            raise TradeRejected('financing is only available for buys')
    pair = await _pair(db, pair_id, lock=True)
    user = await lock_user(db, user_id)
    from app.services.fx.net_trading import reject_net_key
    await reject_net_key(db, user_id, idempotency_key)
    # User 锁后同一 SQL 读取外币欠币及幂等成交。其他 pair 的空头写入
    # 也要先拿 User 锁，因此此快照不会在资金校验前变旧。普通卖出仍走旧查重路径。
    if normalized == "buy":
        short_state = select(
            func.coalesce(func.max(case((
                (FxShortPosition.principal_foreign > 0)
                | (FxShortPosition.interest_foreign > 0), 1,
            ), else_=0)), 0).label("has_debt"),
        ).where(FxShortPosition.user_id == user_id).subquery()
        has_short_debt, old = (await db.execute(
            select(short_state.c.has_debt, FxTrade)
            .select_from(short_state)
            .outerjoin(FxTrade, and_(FxTrade.user_id == user_id,
                                     FxTrade.idempotency_key == idempotency_key))
        )).one()
        has_short_debt = bool(has_short_debt)
    else:
        has_short_debt = False
        old = (await db.execute(select(FxTrade).where(
            FxTrade.user_id == user_id, FxTrade.idempotency_key == idempotency_key,
        ))).scalars().first()
    bot_wallet = None
    if bot_profile_id is not None:
        bot_wallet = await _wallet_lock(db, user_id, pair_id, create=False)
        await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pair_id).with_for_update())
        await _validate_bot(db, user, pair_id, bot_profile_id, bot_purpose, bot_operator_id)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="account is disabled")
    if user.is_bot and bot_profile_id is None:
        raise HTTPException(status_code=403, detail="bot accounts cannot trade FX")
    if old is not None:
        purpose = 'borrow_buy' if financed else 'spot'
        if (old.purpose != purpose or old.pair_id != pair_id or old.side != normalized
                or (old.requested_foreign_amount != amount if amount_mode == 'foreign_out'
                    else old.requested_foreign_amount is not None or old.input_amount != amount)
                or old.min_out != min_out
                or old.borrow_amount != borrow_amount):
            raise HTTPException(status_code=409, detail='idempotency key parameter mismatch')
        return FxTradeExecution(trade=old, public=FxTradePublic.model_validate(old), replay=True)

    if financed:
        if not await site_config.get_bool_or(db, 'loan_enabled', False):
            raise HTTPException(status_code=403, detail='loan_disabled')
    requires_credit = normalized == "buy" and (financed or user.debt > 0 or has_short_debt)
    same_pair_short = False
    if requires_credit:
        credit_deps = await discover_dependencies(db, user_id, extra_groups=[key], account_locked=True)
        # 同一 pair 已欠币时普通现货买入不得建立多头（spec §1/§9），必须走回补
        # 入口；判定放在幂等回放之后，保证重试返回原成交而不是被新规则误拒。
        snapshot = credit_deps.snapshots.get(key)
        if (snapshot is not None and snapshot.short_debt is not None
                and (snapshot.short_debt.principal_foreign > 0
                     or snapshot.short_debt.interest_foreign > 0)):
            same_pair_short = True

    enabled = await site_config.get_bool_or(db, "fx_enabled", False)
    if not enabled:
        raise HTTPException(status_code=403, detail="FX trading is disabled")
    if not _player_side_allowed(pair.status, bool(pair.reduce_only), normalized):
        # Legacy detail preserved for the fully-halted states (paused without
        # reduce_only / draft / closed); reduce-only rejections get a specific
        # reason so the client can explain why buying is unavailable.
        fully_halted = str(pair.status or "").strip().lower() not in ("trading", "paused")
        detail = ("FX pair is not trading" if fully_halted or not bool(pair.reduce_only)
                  else "FX pair is reduce-only")
        raise HTTPException(status_code=403, detail=detail)
    if bot_profile_id is None and user.tos_accepted_at is None:
        raise HTTPException(status_code=403, detail="TOS acceptance required")
    if same_pair_short:
        # 同 pair 单方向：有欠币时普通买入不得建立多头，须走回补入口（spec §1/§9）。
        raise HTTPException(
            status_code=400,
            detail=("this pair has an outstanding short; ordinary buys are "
                    "disabled, use the short-cover entry"),
        )

    try:
        actual = amount
        cash = await available_cash(db, user)
        if bot_profile_id is not None:
            from app.services.pve_fx.config import load_pair_config
            config = await load_pair_config(db, pair_id)
            cap = amount_down(pair.foreign_reserve * config.max_pool_fraction)
            if normalized == 'sell':
                actual = min(amount, cap, bot_wallet.foreign_amount if bot_wallet else Decimal('0'))
            elif amount_mode == 'foreign_out':
                actual = min(amount, cap)
                # Budget quote only determines affordability; execution remains exact-out.
                if cash <= 0:
                    raise TradeRejected('insufficient cash')
                actual = min(actual, quote_buy(cash, pair.gold_reserve, pair.foreign_reserve,
                                               pair.buy_fee_rate).output_amount)
            else:
                # Inventory intent is a fixed-reference-price gold budget.
                if bot_reference_price is None or bot_reference_price <= 0:
                    raise TradeRejected('bot budget buy requires tick reference price')
                actual = min(amount, cash, amount_down(cap * bot_reference_price))
                max_gold = quote_buy_exact_out(cap, pair.gold_reserve, pair.foreign_reserve,
                                               pair.buy_fee_rate).input_amount
                actual = min(actual, max_gold)
        q = (quote_buy_exact_out(actual, pair.gold_reserve, pair.foreign_reserve, pair.buy_fee_rate)
             if amount_mode == 'foreign_out' else _math(pair, normalized, actual))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise TradeRejected(str(exc)) from exc
    if q.output_amount < min_out:
        raise HTTPException(status_code=409, detail="quoted output is below min_out")

    if q.input_amount <= 0 or q.output_amount <= 0:
        raise TradeRejected("trade amount must be positive")
    if normalized == "buy" and await available_cash(db, user) < q.input_amount - (borrow_amount or 0):
        raise HTTPException(status_code=400, detail="insufficient cash")
    wallet = await _wallet_lock(db, user_id, pair_id, create=normalized == "buy")
    trade_now = utcnow()
    if requires_credit:
        holdings = Decimal(wallet.foreign_amount) + q.output_amount
        post_debt = Decimal(user.debt)
        risk_deps = credit_deps
        if financed:
            from app.services.fx.borrow_buy import check_storage_bounds
            post_debt = loan_service.pending_debt(user, credit_deps.daily_rate, trade_now) + borrow_amount
            check_storage_bounds(user, q, borrow_amount, post_debt,
                                 foreign_amount=wallet.foreign_amount, cost_basis=wallet.cost_basis)
            # Only old principal earned pending interest; new principal starts at T.
            risk_deps = replace(credit_deps, debt_last_accrued_at=trade_now)
        decision = await check_new_risk(
            db, user=user, deps=risk_deps,
            post=PostTradeState(
                cash=Decimal(user.cash) + (borrow_amount or 0) - q.input_amount, debt=post_debt,
                fx_reserves={pair_id: (q.post_gold_reserve, q.post_foreign_reserve)},
                post_holdings={key: {pair_id: holdings}},
                base_versions=({key: credit_deps.snapshots[key].version}
                               if key in credit_deps.snapshots else {}),
            ), thresholds=credit_flags.get_flags().thresholds,
            partial_pct=Decimal("1"), now=trade_now, include_valuation=financed,
        )
        if not decision.allowed:
            if decision.reason == "version_conflict":
                raise _RetryCredit()
            raise HTTPException(status_code=400, detail=decision.reason)
        _require_writes()
        before_debt = user.debt
        if financed:
            user = await loan_service.increase_debt(
                db, user_id, borrow_amount, grant_cash=True, daily_rate=credit_deps.daily_rate,
                source='borrow', reason=f'fx_borrow_buy:{pair_id}:{idempotency_key}', now=trade_now)
        else:
            loan_service.accrue_interest(user, credit_deps.daily_rate, trade_now)
        if not financed and user.debt != before_debt:
            audit_service.record(
                db, "interest_accrual", user_id=user.id,
                payload={"debt_before": before_debt, "debt_after": user.debt,
                         "interest": user.debt - before_debt,
                         "daily_rate": credit_deps.daily_rate, "source": "fx_buy"},
                user_after=audit_service.user_snapshot(user))
    return await _write_spot_ledger(db, user=user, pair=pair, wallet=wallet,
        normalized=normalized, q=q, min_out=min_out, idempotency_key=idempotency_key,
        financed=financed, borrow_amount=borrow_amount, trade_now=trade_now,
        requested_foreign_amount=amount if amount_mode == 'foreign_out' else None)


async def _write_spot_ledger(db, *, user, pair, wallet, normalized, q, min_out,
                             idempotency_key, financed=False, borrow_amount=None,
                             trade_now, treasury=None, execution_id=None, execution_leg=None,
                             requested_foreign_amount=None):
    """Post a validated spot leg; caller owns guards, risk and transaction."""
    user_id, pair_id = int(user.id), int(pair.id)
    _require_writes()
    if normalized == "buy":
        user.cash -= q.input_amount
        wallet.foreign_amount += q.output_amount
        wallet.cost_basis += q.input_amount
    else:
        if wallet is None or wallet.foreign_amount < q.input_amount:
            raise HTTPException(status_code=400, detail="insufficient FX wallet balance")
        wallet.foreign_amount -= q.input_amount
        wallet.cost_basis = max(Decimal("0"), wallet.cost_basis - (wallet.cost_basis * q.input_amount / (wallet.foreign_amount + q.input_amount)))
        user.cash += q.output_amount
    wallet.updated_at = utcnow()
    bump_economic_version(user)

    pre_gold, pre_foreign = pair.gold_reserve, pair.foreign_reserve
    pair.gold_reserve, pair.foreign_reserve = q.post_gold_reserve, q.post_foreign_reserve
    pair.pool_version += 1
    pair.updated_at = utcnow()
    stage_fx_pair(db, pair)
    if treasury is None:
        treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pair_id)
                                     .with_for_update().execution_options(populate_existing=True))).scalars().first()
    _require_writes()
    if treasury is None:
        treasury = FxTreasury(pair_id=pair_id)
        db.add(treasury)
    if normalized == "buy":
        treasury.gold_balance += q.fee_amount
    else:
        treasury.foreign_balance += q.fee_amount
    treasury.updated_at = utcnow()
    trade = FxTrade(pair_id=pair_id, user_id=user_id, side=normalized,
                    execution_id=execution_id, execution_leg=execution_leg,
                    purpose='borrow_buy' if financed else 'spot', borrow_amount=borrow_amount,
                    requested_foreign_amount=requested_foreign_amount,
                    input_amount=q.input_amount, output_amount=q.output_amount,
                    min_out=min_out,
                    fee_amount=q.fee_amount, pre_gold_reserve=pre_gold,
                    pre_foreign_reserve=pre_foreign, post_gold_reserve=q.post_gold_reserve,
                    post_foreign_reserve=q.post_foreign_reserve, post_price=q.post_price,
                    source="player", idempotency_key=idempotency_key)
    db.add(trade)
    await db.flush()
    append_pool_event(db, pair_id=trade.pair_id, kind="trade", created_at=trade.created_at,
                      pre_reserves=(trade.pre_gold_reserve, trade.pre_foreign_reserve),
                      post_reserves=(trade.post_gold_reserve, trade.post_foreign_reserve),
                      gold_volume=trade.input_amount if trade.side == "buy" else trade.output_amount,
                      trade_id=trade.id)
    audit_service.record_fx_trade(db, trade=trade, user=user, pair=pair,
                                  wallet=wallet, treasury=treasury)
    if normalized == "sell" and user.auto_repay and user.debt > 0:
        rate = await site_config.get_decimal_or(db, "loan_daily_rate", Decimal("0"))
        debt_before = user.debt
        repaid = await loan_service.decrease_debt_locked(
            db, user, q.output_amount, consume_cash=True, daily_rate=rate, now=trade_now)
        if repaid > 0:
            await ledger_service.record_entry(
                db, user=user, entry_type="repay", cash_delta=-repaid,
                debt_delta=-repaid, daily_rate=rate, reason="fx_sell_proceeds",
                interest_accrued=(user.debt + repaid - debt_before).quantize(Decimal("0.000001")))
    return FxTradeExecution(trade=trade, public=FxTradePublic.model_validate(trade),
                            replay=False)


async def execute_trade(db: AsyncSession, user_id: int, pair_id: int, side: str,
                        amount: Decimal, min_out: Decimal,
                        idempotency_key: str, *, amount_mode: str = 'input',
                        bot_profile_id: int | None = None, bot_purpose: str = 'scheduled',
                        bot_operator_id: int | None = None,
                        bot_reference_price: Decimal | None = None) -> FxTradePublic:
    """Release the producer transaction, then submit immutable request values."""
    from app.services.fx.writer import FX_WRITER
    from app.services.fx.writer_ops import SpotTrade
    _require_writes()
    command = SpotTrade(pair_id=int(pair_id), user_id=int(user_id), side=side,
        amount=_positive(amount, 'amount'), min_out=_nonnegative(min_out, 'min_out'),
        idempotency_key=idempotency_key,
        amount_mode=amount_mode, bot_profile_id=bot_profile_id,
        bot_purpose=bot_purpose, bot_operator_id=bot_operator_id,
        bot_reference_price=bot_reference_price)
    if db.in_transaction():
        await db.rollback()
    return await FX_WRITER.submit(command)


def liquidation_idempotency_key(run_id: int, round_no: int) -> str:
    """Deterministic sell key for one liquidation run round (parent brief §WP4).

    A retry or crash recovery for the same ``(run_id, round_no)`` reuses the
    key, so the ``(user_id, idempotency_key)`` unique constraint on
    ``fx_trade`` turns a double submission into a replay instead of a second
    sale.
    """
    if (not isinstance(run_id, int) or not isinstance(round_no, int)
            or isinstance(run_id, bool) or isinstance(round_no, bool)
            or run_id <= 0 or round_no <= 0):
        raise ValueError("run_id and round_no must be positive integers")
    return f"liq:{run_id}:{round_no}"


async def execute_liquidation_sell_in_session(
    db: AsyncSession,
    *,
    user_id: int,
    pair_id: int,
    run_id: int,
    round_no: int,
    mode: str = "partial",
    partial_pct: Optional[Decimal] = None,
) -> FxLiquidationExecution:
    """Sell one FX batch for forced liquidation inside the caller's transaction.

    Semantics (spec §5.2 / F9 / F12):

    - the batch is **re-quoted under the lock** from the locked pair and wallet
      through ``services.credit.fx_quote.quote_fx_group``; a stale caller-side
      quote is never executed (retry semantics: re-quote, no ``min_out``);
    - ``mode="partial"`` sells ``ceil(balance * partial_pct)`` capped at the
      balance (``partial_pct`` must be passed explicitly); ``mode="full"``
      sells the whole wallet;
    - product executability is the F9 matrix: ``trading`` (either axis) and
      ``paused + reduce_only=true`` are executable; ``paused`` without
      ``reduce_only``, ``draft``, ``closed`` and an invalid/zero-output quote
      are blocked with ``quote.blocked_reason`` and mutate nothing;
    - wallet / cash / pool / treasury / ``FxTrade(source="liquidation")`` /
      audit row are one transaction; the caller repays debt in the same
      transaction and commits once.  This function never commits, never rolls
      back and never publishes.
    - player gates (``fx_enabled`` / bot / TOS / debt) deliberately do not
      apply: forced liquidation is a system action whose gates live in the
      orchestration (``liquidation_enabled``, run state, F9 product state).
    """
    if mode not in ("partial", "full"):
        raise ValueError(f"unknown liquidation mode: {mode!r}")
    if mode == "partial":
        if partial_pct is None:
            raise ValueError("partial_pct is required for partial liquidation")
        # Avoid the binary-float expansion of Decimal(0.1); callers should pass
        # a Decimal but strings/floats must not shift the ceil'd batch.
        partial_pct = (partial_pct if isinstance(partial_pct, Decimal)
                       else Decimal(str(partial_pct)))
        if not (Decimal("0") < partial_pct <= Decimal("1")):
            raise ValueError(f"partial_pct must be in (0, 1]: {partial_pct!r}")
    key = liquidation_idempotency_key(run_id, round_no)

    pair = await _pair(db, pair_id, lock=True)          # 1. product lock
    user = await lock_user(db, user_id)                 # 2. account lock

    old = (await db.execute(select(FxTrade).where(
        FxTrade.user_id == user_id, FxTrade.idempotency_key == key,
    ))).scalars().first()
    if old is not None:
        if (old.pair_id != pair_id or old.side != "sell"
                or old.source != LIQUIDATION_SOURCE):
            raise HTTPException(status_code=409,
                                detail="liquidation idempotency key parameter mismatch")
        # Materialize before returning: callers may roll back (expiring ORM rows).
        return FxLiquidationExecution(
            trade=old, public=FxTradePublic.model_validate(old), quote=None,
            replay=True, blocked_reason=None, idempotency_key=key)

    wallet = await _wallet_lock(db, user_id, pair_id, create=False)  # 3. wallet lock
    foreign_amount = Decimal("0") if wallet is None else Decimal(wallet.foreign_amount)
    quote = quote_fx_group(
        FxPairSnapshot(
            pair_id=pair.id,
            status=str(pair.status),
            reduce_only=bool(pair.reduce_only),
            gold_reserve=Decimal(pair.gold_reserve),
            foreign_reserve=Decimal(pair.foreign_reserve),
            sell_fee_rate=Decimal(pair.sell_fee_rate),
        ),
        foreign_amount=foreign_amount,
        mode=mode,
        partial_pct=partial_pct if mode == "partial" else Decimal("1"),
    )
    if quote.blocked_reason is not None:
        # Blocked batches produce zeros and never touch reserves or balances.
        return FxLiquidationExecution(
            trade=None, public=None, quote=quote, replay=False,
            blocked_reason=quote.blocked_reason, idempotency_key=key)
    if wallet is None or quote.foreign_in > wallet.foreign_amount:
        raise TradeRejected("liquidation quote exceeds the locked wallet balance")

    # 4. treasury lock, then all mutations in the caller's transaction.
    treasury = (await db.execute(
        select(FxTreasury).where(FxTreasury.pair_id == pair_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalars().first()
    if treasury is None:
        treasury = FxTreasury(pair_id=pair_id)
        db.add(treasury)

    _require_writes()
    pre_amount = Decimal(wallet.foreign_amount)
    wallet.foreign_amount = pre_amount - quote.foreign_in
    if wallet.foreign_amount <= 0:
        # A full sale clears the wallet exactly (no fractional dust, F12).
        wallet.foreign_amount = Decimal("0")
        wallet.cost_basis = Decimal("0")
    else:
        wallet.cost_basis = max(
            Decimal("0"),
            wallet.cost_basis - (wallet.cost_basis * quote.foreign_in / pre_amount),
        )
    wallet.updated_at = utcnow()
    user.cash += quote.gold_out
    bump_economic_version(user)

    pre_gold, pre_foreign = pair.gold_reserve, pair.foreign_reserve
    pair.gold_reserve = quote.post_gold_reserve
    pair.foreign_reserve = quote.post_foreign_reserve
    pair.pool_version += 1
    pair.updated_at = utcnow()
    stage_fx_pair(db, pair)
    # Sell fees are charged in foreign inside the AMM; they accrue to treasury
    # while the pool receives the net input.
    treasury.foreign_balance += quote.fee_foreign
    treasury.updated_at = utcnow()

    trade = FxTrade(
        pair_id=pair_id, user_id=user_id, side="sell",
        input_amount=quote.foreign_in, output_amount=quote.gold_out,
        min_out=Decimal("0"), fee_amount=quote.fee_foreign,
        pre_gold_reserve=pre_gold, pre_foreign_reserve=pre_foreign,
        post_gold_reserve=quote.post_gold_reserve,
        post_foreign_reserve=quote.post_foreign_reserve,
        post_price=marginal_price(quote.post_gold_reserve, quote.post_foreign_reserve),
        source=LIQUIDATION_SOURCE, idempotency_key=key,
    )
    db.add(trade)
    await db.flush()
    append_pool_event(db, pair_id=trade.pair_id, kind="trade", created_at=trade.created_at,
                      pre_reserves=(trade.pre_gold_reserve, trade.pre_foreign_reserve),
                      post_reserves=(trade.post_gold_reserve, trade.post_foreign_reserve),
                      gold_volume=trade.input_amount if trade.side == "buy" else trade.output_amount,
                      trade_id=trade.id)
    audit_service.record_fx_trade(db, trade=trade, user=user, pair=pair,
                                  wallet=wallet, treasury=treasury)
    return FxLiquidationExecution(
        trade=trade, public=FxTradePublic.model_validate(trade), quote=quote,
        replay=False, blocked_reason=None, idempotency_key=key)


async def publish_public_event(trade: FxTrade) -> None:
    """Best-effort post-commit publication into the bounded FX publisher.

    Retained for callers/tests that used to await the direct broker write; it
    now only enqueues (never blocks, never raises) instead of performing IO on
    the caller's path.
    """
    publisher.enqueue_publication(
        pair_id=trade.pair_id, post_price=trade.post_price, trade_id=trade.id)


async def get_public_snapshot(db: AsyncSession, pair_id: int, *,
                              pair_state: FxReadState | None = None) -> FxSnapshot:
    pair = pair_state if pair_state is not None else await _read_pair_state(db, pair_id)
    # Stored pairs make the market readable.  If an operator persisted a
    # pathological configuration (fee rate == 1, reserves too small to round a
    # single unit), the AMM math rejects it; surface an actionable 422 for the
    # stored data instead of letting the ValueError bubble up as a 500.
    try:
        price = marginal_price(pair.gold_reserve, pair.foreign_reserve)
        buy_price = _gold_per_foreign(pair, "buy")
        sell_price = _gold_per_foreign(pair, "sell")
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise HTTPException(
            status_code=422,
            detail=("FX pair cannot be quoted because its stored reserves or fee "
                    f"rates are invalid ({exc}); an operator must fix the pair"),
        ) from exc
    # buy_price/sell_price are both gold-per-foreign bid/ask; spread is the
    # non-negative ask-minus-bid cost (fees make it positive, rounding may make
    # it exactly zero for very deep pools).
    spread = buy_price - sell_price
    if spread < Decimal("0"):
        spread = Decimal("0")
    now = utcnow()
    # Exact rolling 24h gold-side volume.  When the incremental history is
    # ready, the read helper composes minute candles + partial-window SQL sums
    # + the projected unflushed tail.  While it is not ready (or the derived
    # state is absent) fall back to the exact single-statement SQL SUM used by
    # Task 1 -- a buy spends ``input_amount`` gold, a sell receives
    # ``output_amount`` gold; ``coalesce`` keeps an empty window at zero.
    volume = await fx_volume_24h(db, pair_id, now)
    if volume is None:
        since = now - timedelta(hours=24)
        gold_volume = func.coalesce(
            func.sum(case(
                (FxTrade.side == "buy", FxTrade.input_amount),
                else_=FxTrade.output_amount,
            )),
            Decimal("0"),
        )
        result = await db.execute(select(gold_volume).where(
            FxTrade.pair_id == pair_id, FxTrade.created_at >= since,
        ))
        raw_volume = result.scalar_one()
        volume = Decimal(raw_volume) if raw_volume is not None else Decimal("0")
    # Additive cached-history bootstrap for the no-per-card-SSE homepage path:
    # absent state yields None/False so the client falls back to /chart rather
    # than trusting a half-built history.
    metadata = await read_fx_snapshot_metadata(db, pair_id, now)
    return FxSnapshot(pair=FxPairPublic.model_validate(pair), price=price,
                      buy_price=buy_price, sell_price=sell_price, spread=spread,
                      volume_24h=volume,
                      history_version=metadata["history_version"],
                      history_ready=metadata["history_ready"])
