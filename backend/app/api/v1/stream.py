# app/api/v1/stream.py
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.base import Market, Outcome
from app.services.lmsr import get_current_price, quantize_price
from app.services.realtime import BROKER, IP_LIMITER, MarketEvent, Subscriber, sse_pack

# SSE 最大连接持续时间（秒）
MAX_SSE_DURATION = 3600  # 1 小时

_logger = logging.getLogger(__name__)

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


def _client_ip(request: Request) -> str:
    """取真实 client IP：nginx 转发优先看 X-Forwarded-For 首段。"""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


@router.get("/market/{market_id}", summary="市场实时流（SSE）")
async def stream_market(market_id: int, request: Request):
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

    # 1) 404 快速预检：response 还未发出，HTTPException 能正确转 404
    async with async_session_maker() as db:
        market = await db.get(Market, market_id)
        if not market:
            raise HTTPException(status_code=404, detail="市场不存在")

    # 2) 503 预检：满了直接返回，避免进 generator 后无法返非 200
    if BROKER.subscriber_count(market_id) >= BROKER.MAX_SUBSCRIBERS_PER_MARKET:
        raise HTTPException(status_code=503, detail="当前市场连接数已满，请稍后重试")

    # 3) 429 per-IP 并发限流：防匿名打满 cap（DDoS surface）
    ip = _client_ip(request)
    if not await IP_LIMITER.try_acquire(market_id, ip):
        raise HTTPException(
            status_code=429,
            detail=f"同一 IP 对单市场的 SSE 并发数已达上限（{IP_LIMITER.MAX_PER_IP}）",
        )

    async def gen() -> AsyncGenerator[bytes, None]:
        # 预检与实际 subscribe 之间可能被填满（极罕见 race）。此时 response 200 已发
        # 无法返 503，只能静默关流，前端 onerror 会触发重连。
        sub: Subscriber
        try:
            # 原子化：subscribe + anchor 在同一把锁内完成。返回 anchor 保证 q 内
            # 后续 event 的 seq 全部 > anchor，client 不再误判 gap_reconnect。
            # （详见 BROKER.subscribe docstring；之前 `current_seq() AFTER subscribe`
            # 的实现有微秒级 race 导致 silent loss。）
            sub, snap_seq_anchor = await BROKER.subscribe(market_id)
        except RuntimeError:
            # cap race；同时释放 IP 计数，不然这台 IP 一次失败白吃一个名额
            await IP_LIMITER.release(market_id, ip)
            return

        kicked_task: asyncio.Task | None = None
        start_time = time.monotonic()
        try:
            # snapshot.data 可能含 seq > anchor 事件的效果（DB commit 抢在 snap.read
            # 之前）；那些事件**同时**会通过 q 流给 client → 看上去是 trade 重复一次。
            # 重复由 trader 侧用 trade_id 去重（DB INSERT OR IGNORE + SseSubscriber
            # 跳 dispatch），可接受。漏失才是真灾难，所以保守选重复。
            async with async_session_maker() as db:
                snap = await _build_snapshot(db, market_id)

            first = MarketEvent(
                type="snapshot",
                market_id=market_id,
                ts=datetime.now(timezone.utc).isoformat(),
                data=snap,
                seq=snap_seq_anchor,
            )
            yield sse_pack(first).encode("utf-8")

            # 循环：等事件、kicked 信号、或超时心跳；任一就绪即推进
            while True:
                if time.monotonic() - start_time > MAX_SSE_DURATION:
                    break

                # 同时 wait queue + kicked Event。kicked 优先 = 慢消费者被踢出
                # 后立即关流，不再卡在 q.get() 假装活着（旧 bug：consumer 死后
                # 只能 yield ping，永久黑屏直到 1h 强断）。
                if kicked_task is None or kicked_task.done():
                    kicked_task = asyncio.create_task(sub.kicked.wait())
                get_task = asyncio.create_task(sub.q.get())

                done, pending = await asyncio.wait(
                    {get_task, kicked_task},
                    timeout=25.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # 优先 kicked
                if kicked_task in done:
                    if not get_task.done():
                        get_task.cancel()
                    _logger.info(
                        f"SSE subscriber kicked for market {market_id} (slow consumer); closing stream"
                    )
                    break

                if get_task in done:
                    evt: MarketEvent = get_task.result()
                    yield sse_pack(evt).encode("utf-8")
                else:
                    # 25s 无 event：取消 get_task，发 ping 防代理超时
                    get_task.cancel()
                    ping = MarketEvent(
                        type="ping",
                        market_id=market_id,
                        ts=datetime.now(timezone.utc).isoformat(),
                        data={},
                    )
                    yield sse_pack(ping).encode("utf-8")
        finally:
            # 避免 "Task was destroyed but it is pending" warning
            if kicked_task is not None and not kicked_task.done():
                kicked_task.cancel()
            await BROKER.unsubscribe(market_id, sub)
            await IP_LIMITER.release(market_id, ip)

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
