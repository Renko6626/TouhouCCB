"""Player-facing FX market and trading endpoints."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.users import current_active_user
from app.models.base import User
from app.models.fx import FxPair, FxTrade, FxWallet
from app.schemas.fx import (
    FxPairPublic,
    FxPersonalTrade,
    FxQuote,
    FxQuoteRequest,
    FxShortCoverRequest,
    FxShortOpenRequest,
    FxShortPositionPublic,
    FxShortQuoteRequest,
    FxShortQuoteResponse,
    FxShortTradeResponse,
    FxSnapshot,
    FxTradePublic,
    FxTradeRequest,
    FxWalletPublic,
)
from app.services.fx import shorts, trading

router = APIRouter()


@router.get("/my-trades", response_model=list[FxPersonalTrade])
async def all_my_trades(
    limit: int = Query(100, ge=1, le=200),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_session),
):
    rows = (await db.execute(
        select(FxTrade, FxPair).join(FxPair, FxPair.id == FxTrade.pair_id)
        .where(FxTrade.user_id == user.id)
        .order_by(FxTrade.created_at.desc(), FxTrade.id.desc()).limit(limit)
    )).all()
    return [FxPersonalTrade(
        **FxTradePublic.model_validate(trade).model_dump(),
        currency_code=pair.currency_code, currency_name=pair.currency_name,
        is_liquidation=trade.source == "liquidation",
    ) for trade, pair in rows]


@router.get("/pairs", response_model=list[FxPairPublic])
async def list_pairs(db: AsyncSession = Depends(get_async_session)):
    rows = (await db.execute(select(FxPair).where(FxPair.status != "draft", FxPair.archived.is_(False)).order_by(FxPair.id))).scalars().all()
    return rows


@router.get("/pairs/{pair_id}/snapshot", response_model=FxSnapshot)
async def snapshot(pair_id: int, db: AsyncSession = Depends(get_async_session)):
    return await trading.get_public_snapshot(db, pair_id)


@router.post("/pairs/{pair_id}/quote", response_model=FxQuote)
async def create_quote(pair_id: int, req: FxQuoteRequest, db: AsyncSession = Depends(get_async_session)):
    try:
        return await trading.quote(db, pair_id, req.side, req.amount)
    except trading.TradeRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/pairs/{pair_id}/trades", response_model=FxTradePublic)
async def create_trade(pair_id: int, req: FxTradeRequest,
                       user: User = Depends(current_active_user),
                       db: AsyncSession = Depends(get_async_session)):
    try:
        return await trading.execute_trade(db, user.id, pair_id, req.side, req.amount,
                                           req.min_out, req.idempotency_key)
    except trading.TradeRejected as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/pairs/{pair_id}/short/open", response_model=FxShortTradeResponse)
async def open_short(pair_id: int, req: FxShortOpenRequest,
                     user: User = Depends(current_active_user),
                     db: AsyncSession = Depends(get_async_session)):
    """Borrow real treasury foreign, sell it and lock the proceeds (spec §7.1)."""
    try:
        return await shorts.execute_short_open(
            db, user_id=user.id, pair_id=pair_id,
            foreign_amount=req.foreign_amount, min_gold_out=req.min_gold_out,
            idempotency_key=req.idempotency_key,
        )
    except shorts.ShortRejected as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except shorts.ShortRetryCredit:
        await db.rollback()
        raise HTTPException(status_code=409, detail="version_conflict; retry") from None


@router.post("/pairs/{pair_id}/short/cover", response_model=FxShortTradeResponse)
async def cover_short(pair_id: int, req: FxShortCoverRequest,
                      user: User = Depends(current_active_user),
                      db: AsyncSession = Depends(get_async_session)):
    """Buy back exactly the requested (or entire) foreign debt (spec §7.2).

    Cover is deliberately its own risk-reducing executor: it is never routed
    through the ordinary spot buy path, works with the opening/loan gates off
    and with credit frozen, and only the total ``fx_enabled=false`` user-trading
    stop or a non-coverable pair state can refuse it.
    """
    try:
        return await shorts.execute_short_cover(
            db, user_id=user.id, pair_id=pair_id,
            foreign_amount=req.foreign_amount, cover_all=req.cover_all,
            max_gold_in=req.max_gold_in, idempotency_key=req.idempotency_key,
        )
    except shorts.ShortRejected as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except shorts.ShortRetryCredit:
        await db.rollback()
        raise HTTPException(status_code=409, detail="version_conflict; retry") from None


@router.get("/pairs/{pair_id}/short", response_model=FxShortPositionPublic)
async def get_short_position(pair_id: int,
                             user: User = Depends(current_active_user),
                             db: AsyncSession = Depends(get_async_session)):
    """Current user's foreign debt for one pair plus a reference cover cost.

    Read-only: the stored interest clock is never advanced and an absent row is
    a zeroed position (like the wallet read), not a 404.  Treasury stock, the
    pair lending cap and other users' rows are never read or exposed.
    """
    return await shorts.read_short_position(db, user_id=user.id, pair_id=pair_id)


@router.post("/pairs/{pair_id}/short/quote", response_model=FxShortQuoteResponse)
async def create_short_quote(pair_id: int, req: FxShortQuoteRequest,
                             user: User = Depends(current_active_user),
                             db: AsyncSession = Depends(get_async_session)):
    """Advisory open/cover quote; the write routes always re-quote under lock."""
    try:
        return await shorts.quote_short(
            db, user_id=user.id, pair_id=pair_id, action=req.action,
            foreign_amount=req.foreign_amount, cover_all=req.cover_all,
        )
    except shorts.ShortRejected as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/pairs/{pair_id}/trades", response_model=list[FxTradePublic])
async def list_trades(pair_id: int, limit: int = Query(50, ge=1, le=100),
                      db: AsyncSession = Depends(get_async_session)):
    return (await db.execute(select(FxTrade).where(FxTrade.pair_id == pair_id)
                             .order_by(FxTrade.created_at.desc()).limit(limit))).scalars().all()


@router.get("/pairs/{pair_id}/wallet", response_model=FxWalletPublic)
async def get_wallet(pair_id: int,
                     user: User = Depends(current_active_user),
                     db: AsyncSession = Depends(get_async_session)):
    """Current user's holding and gold cost basis for one pair.

    Returns a zeroed wallet instead of 404 when the user has never traded the
    pair, so the UI can render an empty position without treating it as an
    error.  Hidden pair controls and other users' wallets are never exposed.
    """
    pair_exists = (await db.execute(select(FxPair.id).where(FxPair.id == pair_id))).scalars().first()
    if pair_exists is None:
        raise HTTPException(status_code=404, detail="FX pair not found")
    wallet = (await db.execute(select(FxWallet).where(
        FxWallet.user_id == user.id, FxWallet.pair_id == pair_id,
    ))).scalars().first()
    if wallet is None:
        return FxWalletPublic(pair_id=pair_id, foreign_amount=Decimal("0"),
                              cost_basis=Decimal("0"), updated_at=None)
    return wallet


@router.get("/pairs/{pair_id}/my-trades", response_model=list[FxTradePublic])
async def list_my_trades(pair_id: int, limit: int = Query(50, ge=1, le=100),
                         user: User = Depends(current_active_user),
                         db: AsyncSession = Depends(get_async_session)):
    """Current user's own FX trades for one pair (newest first)."""
    return (await db.execute(select(FxTrade).where(
        FxTrade.pair_id == pair_id, FxTrade.user_id == user.id,
    ).order_by(FxTrade.created_at.desc()).limit(limit))).scalars().all()
