"""Player FX quotes and atomic trades."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import User
from app.models.fx import FxPair, FxTrade, FxTreasury, FxWallet
from app.schemas.fx import FxQuote, FxSnapshot, FxTradePublic, FxPairPublic
from app.services import audit_service, site_config
from app.services.fx.amm import quote_buy, quote_sell, marginal_price
from app.services.market_locks import lock_user


class TradeRejected(ValueError):
    pass


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


async def execute_trade(db: AsyncSession, user_id: int, pair_id: int, side: str,
                        amount: Decimal, min_out: Decimal,
                        idempotency_key: str) -> FxTradePublic:
    amount = _positive(amount, "amount")
    min_out = _nonnegative(min_out, "min_out")
    if not idempotency_key or len(idempotency_key) > 128:
        raise TradeRejected("idempotency_key is required")

    pair = await _pair(db, pair_id, lock=True)
    user = await lock_user(db, user_id)

    old = (await db.execute(select(FxTrade).where(
        FxTrade.user_id == user_id, FxTrade.idempotency_key == idempotency_key,
    ))).scalars().first()
    if old is not None:
        replay = FxTradePublic.model_validate(old)
        if (old.pair_id != pair_id or old.side != str(side).lower()
                or old.input_amount != amount or old.min_out != min_out):
            await db.rollback()
            raise HTTPException(status_code=409, detail="idempotency key parameter mismatch")
        await db.rollback()
        return replay

    enabled = await site_config.get_bool_or(db, "fx_enabled", False)
    if not enabled:
        raise HTTPException(status_code=403, detail="FX trading is disabled")
    if pair.status != "trading":
        raise HTTPException(status_code=403, detail="FX pair is not trading")
    if user.is_bot:
        raise HTTPException(status_code=403, detail="bot accounts cannot trade FX")
    if user.tos_accepted_at is None:
        raise HTTPException(status_code=403, detail="TOS acceptance required")
    if str(side).lower() == "buy" and user.debt > 0:
        raise HTTPException(status_code=403, detail="outstanding debt blocks FX purchases")

    try:
        q = _math(pair, str(side).lower(), amount)
    except (TypeError, ValueError, ArithmeticError) as exc:
        await db.rollback()
        raise TradeRejected(str(exc)) from exc
    if q.output_amount < min_out:
        await db.rollback()
        raise HTTPException(status_code=409, detail="quoted output is below min_out")

    if q.input_amount <= 0 or q.output_amount <= 0:
        raise TradeRejected("trade amount must be positive")
    wallet = await _wallet_lock(db, user_id, pair_id,
                                create=str(side).lower() == "buy")
    if str(side).lower() == "buy":
        if user.cash < q.input_amount:
            await db.rollback()
            raise HTTPException(status_code=400, detail="insufficient cash")
        user.cash -= q.input_amount
        wallet.foreign_amount += q.output_amount
        wallet.cost_basis += q.input_amount
    else:
        if wallet is None or wallet.foreign_amount < q.input_amount:
            await db.rollback()
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
    if str(side).lower() == "buy":
        treasury.gold_balance += q.fee_amount
    else:
        treasury.foreign_balance += q.fee_amount
    treasury.updated_at = utcnow()
    trade = FxTrade(pair_id=pair_id, user_id=user_id, side=str(side).lower(),
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
    await db.commit()
    await db.refresh(trade)
    await publish_public_event(trade)
    return FxTradePublic.model_validate(trade)


async def publish_public_event(trade: FxTrade) -> None:
    """Post-commit hook for the public FX stream."""
    from app.services.fx.market_data import publish_trade
    await publish_trade(trade)


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
