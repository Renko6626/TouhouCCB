"""Public FX chart and SSE endpoints."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_maker, get_async_session
from app.models.fx import FxPair, FxTrade
from app.services.fx import market_data, trading
from app.services.realtime import BROKER, IP_LIMITER, MarketEvent, Subscriber, sse_pack

router = APIRouter()
MAX_SSE_DURATION = 3600


@router.get("/pairs/{pair_id}/chart")
async def chart(pair_id: int, interval: str = Query("1m"), from_: datetime = Query(..., alias="from"),
                to: datetime = Query(..., alias="to"), db: AsyncSession = Depends(get_async_session)):
    # Database drivers and Python comparisons disagree on mixed naive/aware
    # datetimes; normalize at the API boundary so invalid ranges are 422s.
    from_ = market_data._utc(from_)
    to = market_data._utc(to)
    if from_ >= to:
        raise HTTPException(status_code=422, detail="from must be earlier than to")
    pair = await db.get(FxPair, pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="FX pair not found")
    rows = (await db.execute(select(FxTrade).where(
        FxTrade.pair_id == pair_id, FxTrade.created_at >= from_, FxTrade.created_at < to,
    ).order_by(FxTrade.created_at, FxTrade.id))).scalars().all()
    try:
        candles = market_data.build_price_buckets(rows, interval, from_, to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return [c.__dict__ for c in candles]


def _client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown"))


@router.get("/stream/{pair_id}")
async def stream(pair_id: int, request: Request):
    async with async_session_maker() as db:
        if await db.get(FxPair, pair_id) is None:
            raise HTTPException(status_code=404, detail="FX pair not found")
    if BROKER.subscriber_count(pair_id) >= BROKER.MAX_SUBSCRIBERS_PER_MARKET:
        raise HTTPException(status_code=503, detail="FX stream is full")
    ip = _client_ip(request)
    if not await IP_LIMITER.try_acquire(pair_id, ip):
        raise HTTPException(status_code=429, detail="too many FX streams")

    async def gen() -> AsyncGenerator[bytes, None]:
        sub: Subscriber | None = None
        try:
            sub, anchor = await BROKER.subscribe(pair_id)
            # Anchor first, then read state. Any commit after the anchor is
            # either reflected in this snapshot or remains queued for replay.
            async with async_session_maker() as db:
                snapshot = await trading.get_public_snapshot(db, pair_id)
            initial = MarketEvent("snapshot", pair_id, datetime.now(timezone.utc).isoformat(),
                                  market_data.public_frame_to_wire(
                                      market_data.build_public_frame(snapshot)), anchor)
            yield sse_pack(initial).encode()
            started = time.monotonic()
            while time.monotonic() - started < MAX_SSE_DURATION:
                get_task = asyncio.create_task(sub.q.get())
                kicked_task = asyncio.create_task(sub.kicked.wait())
                done, pending = await asyncio.wait({get_task, kicked_task}, timeout=25,
                                                   return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if kicked_task in done:
                    break
                if get_task in done:
                    yield get_task.result()
                else:
                    yield b": ping\n\n"
        finally:
            if sub is not None:
                await BROKER.unsubscribe(pair_id, sub)
            await IP_LIMITER.release(pair_id, ip)

    return StreamingResponse(gen(), media_type="text/event-stream")
