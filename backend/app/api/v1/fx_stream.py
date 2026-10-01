"""Public FX chart and SSE endpoints."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_maker, get_async_session
from app.models.fx import FxPair
from app.services.credit.keys import symbol_namespace
from app.services.fx import market_data, trading
from app.services.fx.market_reads import read_fx_chart_with_meta
from app.services.fx.market_state import FX_MARKET_DATA
from app.services.realtime import BROKER, IP_LIMITER, MarketEvent, Subscriber, sse_pack

router = APIRouter()
MAX_SSE_DURATION = 3600
FX_THROUGH_HEADER = "X-FX-Through-Trade-ID"
FX_VERSION_HEADER = "X-FX-History-Version"


@router.get("/pairs/{pair_id}/chart")
async def chart(pair_id: int, response: Response, interval: str = Query("1m"),
                from_: datetime = Query(..., alias="from"),
                to: datetime = Query(..., alias="to"),
                db: AsyncSession = Depends(get_async_session)):
    # Validation, ring read and the materialised-candle + unflushed-tail merge
    # all live in the read helper.  The body keeps the old response shape
    # (bucket_start/interval/open/high/low/close/volume) and the exact coverage
    # metadata travels in headers so a cached-history fallback cannot stamp an
    # old SSE cursor onto newer rows.  An unfinished history is an explicit
    # retryable 503, never a raw full-day trade rebuild.
    body, through_trade_id, history_version = await read_fx_chart_with_meta(
        db, pair_id, interval, from_, to)
    response.headers[FX_THROUGH_HEADER] = str(through_trade_id)
    response.headers[FX_VERSION_HEADER] = str(history_version)
    return body


def _client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown"))


@router.get("/stream/{pair_id}")
async def stream(pair_id: int, request: Request):
    # WP8a：FX 行情 topic 带产品前缀，与同号 LMSR market 的流彻底隔离。
    topic = symbol_namespace("fx", int(pair_id))
    async with async_session_maker() as db:
        if await db.get(FxPair, pair_id) is None:
            raise HTTPException(status_code=404, detail="FX pair not found")
    if BROKER.subscriber_count(topic) >= BROKER.MAX_SUBSCRIBERS_PER_MARKET:
        raise HTTPException(status_code=503, detail="FX stream is full")
    ip = _client_ip(request)
    if not await IP_LIMITER.try_acquire(topic, ip):
        raise HTTPException(status_code=429, detail="too many FX streams")

    async def gen() -> AsyncGenerator[bytes, None]:
        sub: Subscriber | None = None
        try:
            sub, anchor = await BROKER.subscribe(topic)
            # Anchor first, then read state. Any commit after the anchor is
            # either reflected in this snapshot or remains queued for replay.
            async with async_session_maker() as db:
                snapshot = await trading.get_public_snapshot(db, pair_id)
            now = datetime.now(timezone.utc)
            # The first packet carries the current ring tail (version, readiness
            # and per-interval segments) plus the coverage cursor.  Trades that
            # overlap the tail arrive as later ``fx`` deltas and are de-duplicated
            # by the client against ``history_tail_through_trade_id``.
            history = FX_MARKET_DATA.tail(pair_id, now)
            wire = market_data.public_frame_to_wire(
                market_data.build_public_envelope(snapshot, history=history))
            initial = MarketEvent("snapshot", pair_id, now.isoformat(), wire, anchor)
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
                await BROKER.unsubscribe(topic, sub)
            await IP_LIMITER.release(topic, ip)

    return StreamingResponse(gen(), media_type="text/event-stream")
