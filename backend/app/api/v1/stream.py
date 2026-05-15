# app/api/v1/stream.py
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from starlette.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import Market, Outcome
from app.services.lmsr import get_current_price, quantize_price
from app.services.realtime import BROKER, MarketEvent, sse_pack

# SSE 最大连接持续时间（秒）
MAX_SSE_DURATION = 3600  # 1 小时

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
        price = quantize_price(get_current_price(shares_list, i, b))
        is_winner = None
        if getattr(market, "winning_outcome_id", None) is not None:
            is_winner = (int(market.winning_outcome_id) == int(o.id))

        out_reads.append({
            "id": int(o.id),
            "label": str(o.label),
            "total_shares": float(o.total_shares),
            "current_price": float(price),
            "payout": float(o.payout) if o.payout is not None else None,
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
async def stream_market(market_id: int):
    """
    SSE 输出：
    - 首包：snapshot（市场当前状态+outcomes现价）
    - 后续：trade / market_status
    - 心跳：ping

    注意：SSE 连接可持续到 MAX_SSE_DURATION（1h），且 snapshot 之后不再用 DB。
    所以不走 Depends(get_async_session) — 那会让一个 DB 连接被这次请求独占整段连接
    时长，500 并发 SSE 就能把 pool 打爆。这里用临时 sessionmaker 取完 snapshot 立刻
    归还连接，broker 队列纯内存推送不需要 DB。
    """

    # 取 snapshot —— 用临时 session，出了 with 块连接立即归还 pool
    async with async_session_maker() as db:
        snap = await _build_snapshot(db, market_id)  # 内部已处理 404 / 400

    # 503 预检：满了直接返回，此时 response 还未发出，HTTPException 可正确转 503。
    # 不在这里实际 subscribe —— 否则若客户端在 generator 启动前就断开，
    # 订阅会泄漏（finally 永远不进入）。subscribe 移到 generator 内 try 头部，
    # finally 必然 cover unsubscribe。
    if BROKER.subscriber_count(market_id) >= BROKER.MAX_SUBSCRIBERS_PER_MARKET:
        raise HTTPException(status_code=503, detail="当前市场连接数已满，请稍后重试")

    async def gen() -> AsyncGenerator[bytes, None]:
        # 预检与实际 subscribe 之间可能被填满（极罕见 race）。此时 response 200 已发
        # 无法返 503，只能静默关流，前端 onerror 会触发重连。
        try:
            q = await BROKER.subscribe(market_id)
        except RuntimeError:
            return

        start_time = time.monotonic()
        try:
            # snapshot 携带当前 broker.current_seq 作为客户端 lastSeq 锚点。
            # 后续真实事件 seq 单调递增，客户端用它检测 gap。
            first = MarketEvent(
                type="snapshot",
                market_id=market_id,
                ts=datetime.now(timezone.utc).isoformat(),
                data=snap,
                seq=BROKER.current_seq(market_id),
            )
            yield sse_pack(first).encode("utf-8")

            # 循环：等事件或心跳，超过最大时长后断开
            while True:
                if time.monotonic() - start_time > MAX_SSE_DURATION:
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=25.0)
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
