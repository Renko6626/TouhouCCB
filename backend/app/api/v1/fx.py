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
    FxQuote,
    FxQuoteRequest,
    FxSnapshot,
    FxTradePublic,
    FxTradeRequest,
    FxWalletPublic,
)
from app.services.fx import trading

router = APIRouter()


@router.get("/pairs", response_model=list[FxPairPublic])
async def list_pairs(db: AsyncSession = Depends(get_async_session)):
    rows = (await db.execute(select(FxPair).where(FxPair.status != "draft").order_by(FxPair.id))).scalars().all()
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
