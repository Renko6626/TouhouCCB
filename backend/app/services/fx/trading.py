"""Player FX quotes and atomic trades.

Transaction contract (WP4a):

- ``quote`` / ``get_public_snapshot`` are pure reads: no writes, no commit.
- ``execute_trade_in_session`` performs every validation, lock and balance
  mutation for a player trade **inside the caller's transaction**.  It never
  commits, never rolls back and never publishes, so the same core can be
  reused by a caller that owns one larger transaction.
- ``execute_trade`` is the request-path wrapper: it owns commit (or the
  idempotent-replay rollback) and hands the committed trade to the bounded
  post-commit publisher, which never blocks the response on the public frame
  or on the 24h volume aggregation.

Lock order is always ``pair -> user -> wallet -> treasury``; the in-session
function keeps it identical to the historical implementation.
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

from app.models.base import User
from app.models.fx import FxPair, FxTrade, FxTreasury, FxWallet
from app.schemas.fx import FxQuote, FxSnapshot, FxTradePublic, FxPairPublic
from app.services import audit_service, site_config
from app.services.fx import publisher
from app.services.fx.amm import quote_buy, quote_sell, marginal_price
from app.services.market_locks import lock_user

_logger = logging.getLogger(__name__)


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
                                   idempotency_key: str) -> FxTradeExecution:
    """Execute one player FX trade inside the caller's transaction.

    Keeps every historical guard, fee and idempotency binding of
    ``execute_trade`` (identical HTTP status codes), acquires the product locks
    in ``pair -> user -> wallet -> treasury`` order and flushes its writes, but
    deliberately does **not** commit, roll back or publish.  On error the
    caller owns the transaction and must roll it back before reuse.
    """
    amount = _positive(amount, "amount")
    min_out = _nonnegative(min_out, "min_out")
    if not idempotency_key or len(idempotency_key) > 128:
        raise TradeRejected("idempotency_key is required")

    normalized = str(side).lower()
    pair = await _pair(db, pair_id, lock=True)
    user = await lock_user(db, user_id)

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
    if pair.status != "trading":
        raise HTTPException(status_code=403, detail="FX pair is not trading")
    if user.is_bot:
        raise HTTPException(status_code=403, detail="bot accounts cannot trade FX")
    if user.tos_accepted_at is None:
        raise HTTPException(status_code=403, detail="TOS acceptance required")
    if normalized == "buy" and user.debt > 0:
        raise HTTPException(status_code=403, detail="outstanding debt blocks FX purchases")

    try:
        q = _math(pair, normalized, amount)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise TradeRejected(str(exc)) from exc
    if q.output_amount < min_out:
        raise HTTPException(status_code=409, detail="quoted output is below min_out")

    if q.input_amount <= 0 or q.output_amount <= 0:
        raise TradeRejected("trade amount must be positive")
    wallet = await _wallet_lock(db, user_id, pair_id, create=normalized == "buy")
    if normalized == "buy":
        if user.cash < q.input_amount:
            raise HTTPException(status_code=400, detail="insufficient cash")
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

    pre_gold, pre_foreign = pair.gold_reserve, pair.foreign_reserve
    pair.gold_reserve, pair.foreign_reserve = q.post_gold_reserve, q.post_foreign_reserve
    pair.pool_version += 1
    pair.updated_at = utcnow()
    treasury = (await db.execute(select(FxTreasury).where(FxTreasury.pair_id == pair_id)
                                 .with_for_update().execution_options(populate_existing=True))).scalars().first()
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
    try:
        execution = await execute_trade_in_session(
            db, user_id, pair_id, side, amount, min_out, idempotency_key)
    except BaseException:
        await _rollback_quietly(db)
        raise
    if execution.replay:
        await db.rollback()
        return execution.public
    await db.commit()
    await db.refresh(execution.trade)
    public = FxTradePublic.model_validate(execution.trade)
    publisher.enqueue_publication(
        pair_id=execution.trade.pair_id,
        post_price=execution.trade.post_price,
        trade_id=execution.trade.id,
    )
    return public


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
