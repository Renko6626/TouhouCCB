# app/api/v1/stream.py
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_async_session
from app.models.base import Market, Outcome
from app.schemas.market import MarketDetailRead, OutcomeQuoteRead
from app.services.lmsr import get_current_price
from app.services.realtime import BROKER, MarketEvent, sse_pack

router = APIRouter()

async def _build_snapshot(db: AsyncSession, market_id: int) -> dict:
    market = await db.get(Market, market_id)
    if not market:
        raise HTTPException(status_code=404, detail="市场不存在")

    o_res = await db.execute(
        select(Outcome).where(Outcome.market_id == market.id).order_by(Outcome.id.asc())
    )
    outcomes = o_res.scalars().all()
    if not outcomes:
        raise HTTPException(status_code=400, detail="市场选项异常：无 outcomes")

    shares_list = [float(o.total_shares) for o in outcomes]
    b = float(market.liquidity_b)

    out_reads = []
    for i, o in enumerate(outcomes):
        price = float(get_current_price(shares_list, i, b))
        is_winner = None
        if getattr(market, "winning_outcome_id", None) is not None:
            is_winner = (int(market.winning_outcome_id) == int(o.id))

        out_reads.append({
            "id": int(o.id),
            "label": str(o.label),
            "total_shares": float(o.total_shares),
            "current_price": round(price, 6),
            "payout": getattr(o, "payout", None),
            "is_winner": is_winner,
        })

    created_at = market.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    return {
        "id": int(market.id),
        "title": str(market.title),
        "description": str(market.description or ""),
        "status": str(market.status),
        "liquidity_b": float(market.liquidity_b),
        "created_at": created_at.isoformat(),
        "winning_outcome_id": getattr(market, "winning_outcome_id", None),
        "settled_at": getattr(market, "settled_at", None).isoformat() if getattr(market, "settled_at", None) else None,
        "settled_by_user_id": getattr(market, "settled_by_user_id", None),
        "outcomes": out_reads,
    }


@router.get("/market/{market_id}", summary="市场实时流（SSE）")
async def stream_market(
    market_id: int,
    db: AsyncSession = Depends(get_async_session),
):
    """
    SSE 输出：
    - 首包：snapshot（市场当前状态+outcomes现价）
    - 后续：trade / market_status
    - 心跳：ping
    """

    # 先确认市场存在（也避免无效订阅占资源）
    market = await db.get(Market, market_id)
    if not market:
        raise HTTPException(status_code=404, detail="市场不存在")

    async def gen() -> AsyncGenerator[bytes, None]:
        # 订阅
        q = await BROKER.subscribe(market_id)
        try:
            # 先发 snapshot
            snap = await _build_snapshot(db, market_id)
            first = MarketEvent(
                type="snapshot",
                market_id=market_id,
                ts=datetime.now(timezone.utc).isoformat(),
                data=snap,
            )
            yield sse_pack(first).encode("utf-8")

            # 循环：等事件或心跳
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield sse_pack(evt).encode("utf-8")
                except asyncio.TimeoutError:
                    ping = MarketEvent(
                        type="ping",
                        market_id=market_id,
                        ts=datetime.now(timezone.utc).isoformat(),
                        data={},
                    )
                    yield sse_pack(ping).encode("utf-8")
        finally:
            await BROKER.unsubscribe(market_id, q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 反代如 Nginx 建议关闭缓冲，否则 SSE 会被攒包
            "X-Accel-Buffering": "no",
        },
    )
