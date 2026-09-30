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
- ``execute_trade`` is the request-path wrapper: it owns commit (or the
  idempotent-replay rollback) and hands the committed trade to the bounded
  post-commit publisher, which never blocks the response on the public frame
  or on the 24h volume aggregation.

Lock order is always ``pair -> user -> wallet -> treasury`` in all three
execution paths.  Player order acceptance follows the F9 double-axis product
state (``trading`` / ``paused`` x ``reduce_only``); the full matrix is
documented on :func:`_player_side_allowed`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.credit.cash import available_cash, has_foreign_debt
from app.models.base import User
from app.models.fx import FxPair, FxTrade, FxTreasury, FxWallet
from app.schemas.fx import FxQuote, FxSnapshot, FxTradePublic, FxPairPublic
from app.services import audit_service, site_config, loan_service, ledger_service
from app.services.credit.fx_quote import FxGroupQuote, FxPairSnapshot, quote_fx_group
from app.services.fx import publisher
from app.services.fx.amm import quote_buy, quote_sell, marginal_price
from app.services.market_locks import lock_user
from app.services.credit import flags as credit_flags
from app.services.credit.gates import GATES
from app.services.credit.keys import GroupKey
from app.services.credit.ownership import OWNERSHIP
from app.services.credit.risk import DependencySet, PostTradeState, discover_dependencies, check_new_risk
from app.services.credit.version import bump_economic_version, economic_version_of

_logger = logging.getLogger(__name__)

#: ``FxTrade.source`` for forced-liquidation sells (WP4 parent brief §WP4).
LIQUIDATION_SOURCE = "liquidation"


class _RetryCredit(Exception):
    """Release transaction and all gates before discovering dependencies again."""


def _require_writes():
    flags = credit_flags.get_flags()
    if (flags.unified_credit_enabled or flags.read_only_instance
            or credit_flags.read_only_from_env() or OWNERSHIP.reason is not None):
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


async def _pair(db: AsyncSession, pair_id: int, *, lock: bool = False) -> FxPair:
    stmt = select(FxPair).where(FxPair.id == pair_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    pair = (await db.execute(stmt)).scalars().first()
    if pair is None:
        raise HTTPException(status_code=404, detail="FX pair not found")
    return pair


def _math(pair: FxPair, side: str, amount: Decimal):
    side = str(side).lower()
    if side == "buy":
        return quote_buy(amount, pair.gold_reserve, pair.foreign_reserve, pair.buy_fee_rate)
    if side == "sell":
        return quote_sell(amount, pair.gold_reserve, pair.foreign_reserve, pair.sell_fee_rate)
    raise TradeRejected("side must be buy or sell")


def _gold_per_foreign(pair: FxPair, side: str) -> Decimal:
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
    pair = await _pair(db, pair_id)
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
        select(FxWallet).where(FxWallet.user_id == user_id, FxWallet.pair_id == pair_id)
        .with_for_update().execution_options(populate_existing=True)
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


async def execute_trade_in_session(db: AsyncSession, user_id: int, pair_id: int, side: str,
                                   amount: Decimal, min_out: Decimal,
                                   idempotency_key: str, *,
                                   credit_deps: Optional[DependencySet] = None) -> FxTradeExecution:
    """Execute one player FX trade inside the caller's transaction.

    Unified callers must acquire the target exclusive gate and every collateral
    shared gate before opening this transaction, pass ``credit_deps`` for debt
    buys, and retain gates through commit. ``_RetryCredit`` requires rollback,
    release of all gates and fresh discovery; never retry inside existing gates.
    Flag-off keeps the historical debt-buy prohibition. Acquires product locks
    in ``pair -> user -> wallet -> treasury`` order and flushes its writes, but
    deliberately does **not** commit, roll back or publish.  On error the
    caller owns the transaction and must roll it back before reuse.
    """
    _require_writes()
    unified = credit_flags.get_flags().unified_credit_enabled
    key = GroupKey("fx", pair_id)
    if unified and key not in GATES.held_keys_by_current_task():
        raise RuntimeError("unified FX caller must hold target exclusive gate through commit")
    amount = _positive(amount, "amount")
    min_out = _nonnegative(min_out, "min_out")
    if not idempotency_key or len(idempotency_key) > 128:
        raise TradeRejected("idempotency_key is required")
    if idempotency_key.startswith("liq:"):
        raise TradeRejected("idempotency_key prefix is reserved")

    normalized = str(side).lower()
    pair = await _pair(db, pair_id, lock=True)
    user = await lock_user(db, user_id)
    # 无金债不再等于无风险（spec §6.3/§11）：乐观首试在 User 锁内按索引确认外币
    # 欠币，存在欠币则释放锁与门闩、重新发现完整依赖后带 GATES 重试。金债>0 时
    # 短路，不额外查询外币表，保持旧的债务人路径 SQL 形状。
    requires_credit = unified and normalized == "buy" and (
        user.debt > 0 or await has_foreign_debt(db, user_id)
    )
    same_pair_short = False
    if requires_credit:
        if (credit_deps is None
                or economic_version_of(user) != credit_deps.economic_version
                or not set(credit_deps.groups).issubset(GATES.held_keys_by_current_task())):
            raise _RetryCredit()
        # Price snapshots from discovery can age while waiting for gates. Refresh
        # inside the complete gate set before simulating this transaction.
        credit_deps = await discover_dependencies(db, user_id, extra_groups=[key])
        # 锁内重发现若引入当前 GATE 集合之外的新依赖组（例如并发新增的外币空头/
        # 资产 pair，即使 User economic_version 未变），其快照没有门闩保护，绝不能用
        # 于风险报价：退出让外层释放锁与 GATES 后重新发现并一次性取全有序门闩。
        # 禁止在持 User 锁时补拿新的 pair GATE（spec §11）。
        if not set(credit_deps.groups).issubset(GATES.held_keys_by_current_task()):
            raise _RetryCredit()
        # 同一 pair 已欠币时普通现货买入不得建立多头（spec §1/§9），必须走回补
        # 入口；判定放在幂等回放之后，保证重试返回原成交而不是被新规则误拒。
        snapshot = credit_deps.snapshots.get(key)
        if (snapshot is not None and snapshot.short_debt is not None
                and (snapshot.short_debt.principal_foreign > 0
                     or snapshot.short_debt.interest_foreign > 0)):
            same_pair_short = True

    old = (await db.execute(select(FxTrade).where(
        FxTrade.user_id == user_id, FxTrade.idempotency_key == idempotency_key,
    ))).scalars().first()
    if old is not None:
        if (old.pair_id != pair_id or old.side != normalized
                or old.input_amount != amount or old.min_out != min_out):
            raise HTTPException(status_code=409, detail="idempotency key parameter mismatch")
        # Materialize before returning: the wrapper rolls back to release the
        # pair/user locks, which expires ORM instances.
        return FxTradeExecution(trade=old, public=FxTradePublic.model_validate(old),
                                replay=True)

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
    if user.is_bot:
        raise HTTPException(status_code=403, detail="bot accounts cannot trade FX")
    if user.tos_accepted_at is None:
        raise HTTPException(status_code=403, detail="TOS acceptance required")
    if same_pair_short:
        # 同 pair 单方向：有欠币时普通买入不得建立多头，须走回补入口（spec §1/§9）。
        raise HTTPException(
            status_code=400,
            detail=("this pair has an outstanding short; ordinary buys are "
                    "disabled, use the short-cover entry"),
        )
    if not unified and normalized == "buy" and user.debt > 0:
        raise HTTPException(status_code=403, detail="outstanding debt blocks FX purchases")

    try:
        q = _math(pair, normalized, amount)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise TradeRejected(str(exc)) from exc
    if q.output_amount < min_out:
        raise HTTPException(status_code=409, detail="quoted output is below min_out")

    if q.input_amount <= 0 or q.output_amount <= 0:
        raise TradeRejected("trade amount must be positive")
    if normalized == "buy" and await available_cash(db, user) < q.input_amount:
        raise HTTPException(status_code=400, detail="insufficient cash")
    wallet = await _wallet_lock(db, user_id, pair_id, create=normalized == "buy")
    trade_now = utcnow()
    if requires_credit:
        holdings = Decimal(wallet.foreign_amount) + q.output_amount
        decision = await check_new_risk(
            db, user=user, deps=credit_deps,
            post=PostTradeState(
                cash=Decimal(user.cash) - q.input_amount, debt=Decimal(user.debt),
                fx_reserves={pair_id: (q.post_gold_reserve, q.post_foreign_reserve)},
                post_holdings={key: {pair_id: holdings}},
                base_versions=({key: credit_deps.snapshots[key].version}
                               if key in credit_deps.snapshots else {}),
            ), thresholds=credit_flags.get_flags().thresholds,
            partial_pct=Decimal("1"), now=trade_now,
        )
        if not decision.allowed:
            if decision.reason == "version_conflict":
                raise _RetryCredit()
            raise HTTPException(status_code=400, detail=decision.reason)
        _require_writes()
        before_debt = user.debt
        loan_service.accrue_interest(user, credit_deps.daily_rate, trade_now)
        if user.debt != before_debt:
            audit_service.record(
                db, "interest_accrual", user_id=user.id,
                payload={"debt_before": before_debt, "debt_after": user.debt,
                         "interest": user.debt - before_debt,
                         "daily_rate": credit_deps.daily_rate, "source": "fx_buy"},
                user_after=audit_service.user_snapshot(user))
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
                    input_amount=q.input_amount, output_amount=q.output_amount,
                    min_out=min_out,
                    fee_amount=q.fee_amount, pre_gold_reserve=pre_gold,
                    pre_foreign_reserve=pre_foreign, post_gold_reserve=q.post_gold_reserve,
                    post_foreign_reserve=q.post_foreign_reserve, post_price=q.post_price,
                    source="player", idempotency_key=idempotency_key)
    db.add(trade)
    await db.flush()
    audit_service.record_fx_trade(db, trade=trade, user=user, pair=pair,
                                  wallet=wallet, treasury=treasury)
    if unified and normalized == "sell" and user.debt > 0:
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
                        idempotency_key: str) -> FxTradePublic:
    """Request-path wrapper: owns the transaction boundary and publication.

    Commits a fresh execution (or rolls back an idempotent replay to release
    the pair/user locks) and enqueues the committed trade on the bounded
    publisher.  Enqueueing never blocks: the response does not wait for the
    public frame or its 24h volume aggregation.
    """
    _require_writes()
    flags = credit_flags.get_flags()
    if not flags.unified_credit_enabled:
        try:
            execution = await execute_trade_in_session(
                db, user_id, pair_id, side, amount, min_out, idempotency_key)
            if execution.replay:
                await db.rollback()
                return execution.public
            _require_writes()
            await db.commit()
        except BaseException:
            await _rollback_quietly(db)
            raise
    else:
        needs_discovery = False
        for attempt in range(flags.credit_risk_retry_limit + 2):
            try:
                # Optimistically take only the target gate. If the locked user
                # has debt, release everything and discover the complete set.
                deps = (await discover_dependencies(db, user_id, extra_groups=[GroupKey("fx", pair_id)])
                        if needs_discovery else None)
                if db.in_transaction():
                    await db.rollback()
                async with GATES.hold(exclusive=[GroupKey("fx", pair_id)],
                                      shared=deps.groups if deps else ()):
                    try:
                        execution = await execute_trade_in_session(
                            db, user_id, pair_id, side, amount, min_out,
                            idempotency_key, credit_deps=deps)
                        if execution.replay:
                            await db.rollback()
                            return execution.public
                        _require_writes()
                        await db.commit()
                    except BaseException:
                        await _rollback_quietly(db)
                        raise
                break
            except _RetryCredit:
                needs_discovery = True
                if attempt == flags.credit_risk_retry_limit + 1:
                    raise HTTPException(status_code=409, detail="version_conflict; retry")
            except BaseException:
                await _rollback_quietly(db)
                raise
    # Pool version is committed before gates release; publication is bounded
    # and cannot extend the transaction or hold unrelated symbols.
    await db.refresh(execution.trade)
    public = FxTradePublic.model_validate(execution.trade)
    publisher.enqueue_publication(pair_id=public.pair_id, post_price=public.post_price,
                                  trade_id=public.id)
    return public


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


async def get_public_snapshot(db: AsyncSession, pair_id: int) -> FxSnapshot:
    pair = await _pair(db, pair_id)
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
    since = utcnow() - timedelta(hours=24)
    # A public volume value is informational and uses gold-side input/output units.
    result = await db.execute(select(FxTrade).where(
        FxTrade.pair_id == pair_id, FxTrade.created_at >= since,
    ))
    volume = sum((t.input_amount if t.side == "buy" else t.output_amount for t in result.scalars()), Decimal("0"))
    return FxSnapshot(pair=FxPairPublic.model_validate(pair), price=price,
                      buy_price=buy_price, sell_price=sell_price, spread=spread,
                      volume_24h=volume)
