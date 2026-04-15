# app/api/v1/chart.py

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Dict, Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.models.base import Transaction
from app.schemas.chart import (
    PricePoint,
    Candle,
    PriceSeriesResponse,
    CandleSeriesResponse,
    Interval,
)

router = APIRouter()

_INTERVAL_SECONDS: Dict[str, int] = {
    "10s": 10,
    "30s": 30,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "1d": 86400,
}


def _ensure_utc(dt: datetime) -> datetime:
    """统一成 aware UTC。naive datetime 视为 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _bucket_start(ts: datetime, step_seconds: int) -> datetime:
    ts = _ensure_utc(ts)
    epoch = int(ts.timestamp())
    bucket = epoch - (epoch % step_seconds)
    return datetime.fromtimestamp(bucket, tz=timezone.utc)


def _align_range_to_buckets(from_ts: datetime, to_ts: datetime, step: int) -> tuple[datetime, datetime]:
    """将 from/to 对齐到桶边界：from 向下取整，to 向上取整（闭区间查询仍然 OK）。"""
    f = _bucket_start(from_ts, step)
    t0 = _ensure_utc(to_ts)
    # 向上取整：如果 to 本身不在边界，进到下一个桶开始
    to_epoch = int(t0.timestamp())
    if to_epoch % step != 0:
        to_epoch = to_epoch + (step - (to_epoch % step))
    t = datetime.fromtimestamp(to_epoch, tz=timezone.utc)
    return f, t


def _tx_price(tx: Transaction) -> float:
    """Price series: prefer post_market_price > price > cost/shares."""
    for attr in ("post_market_price", "market_price", "price"):
        v = getattr(tx, attr, None)
        if v is not None and float(v) > 0:
            return float(v)
    if tx.shares and float(tx.shares) > 0:
        return float(abs(tx.cost) / float(tx.shares))
    return 0.0


def _validate_range(from_ts_u: datetime, to_ts_u: datetime) -> None:
    if to_ts_u <= from_ts_u:
        raise HTTPException(status_code=400, detail="to_ts 必须大于 from_ts")


def _extract_prices(pre_mp: float, post_mp: float, price: float, cost: float, shares: float) -> tuple[float, float]:
    """
    从一笔交易中提取 K 线的 open/close 价格。
    open = 交易前市场价 (pre_market_price)
    close = 交易后市场价 (post_market_price)
    回退到旧字段兼容历史数据。
    """
    def _fallback() -> float:
        if price > 0:
            return price
        if shares > 0:
            return abs(cost) / shares
        return 0.0

    o = pre_mp if pre_mp > 0 else _fallback()
    c = post_mp if post_mp > 0 else _fallback()
    return o, c


@router.get("/price", response_model=PriceSeriesResponse, summary="价格曲线（成交价序列）")
async def get_price_series(
    outcome_id: int = Query(..., description="Outcome ID"),
    from_ts: datetime = Query(..., description="起始时间（ISO）"),
    to_ts: datetime = Query(..., description="结束时间（ISO）"),
    limit: int = Query(5000, ge=1, le=20000, description="最多返回点数"),
    bucket: Optional[Interval] = Query(
        None,
        description="可选：按桶降采样（如 10s/30s/1m）。传入后每个桶只返回最后一笔成交价。",
    ),
    db: AsyncSession = Depends(get_async_session),
):
    """
    返回成交价点序列（按时间排序）。
    - 默认返回所有交易点（受 limit 控制）
    - 传 bucket 后：每桶取最后一笔价（更适合画线，点更少）
    """
    from_ts_u = _ensure_utc(from_ts)
    to_ts_u = _ensure_utc(to_ts)
    _validate_range(from_ts_u, to_ts_u)

    # 先把交易捞出来（方案A：只靠 Transaction）
    stmt = (
        select(Transaction)
        .where(
            and_(
                Transaction.outcome_id == outcome_id,
                Transaction.timestamp >= from_ts_u,
                Transaction.timestamp <= to_ts_u,
            )
        )
        .order_by(Transaction.timestamp.asc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    txs: List[Transaction] = res.scalars().all()

    if not bucket:
        points: List[PricePoint] = []
        for tx in txs:
            p = _tx_price(tx)
            if p > 0:
                points.append(PricePoint(ts=_ensure_utc(tx.timestamp), price=p))
        return PriceSeriesResponse(
            outcome_id=outcome_id,
            from_ts=from_ts_u,
            to_ts=to_ts_u,
            points=points,
        )

    # 按桶降采样：每个桶保留最后一笔
    step = _INTERVAL_SECONDS[str(bucket)]
    buckets: Dict[datetime, PricePoint] = {}
    for tx in txs:
        p = _tx_price(tx)
        if p <= 0:
            continue
        ts = _ensure_utc(tx.timestamp)
        b0 = _bucket_start(ts, step)
        # 因为 txs 已按时间升序，直接覆盖即可，最后就是桶内最后一笔
        buckets[b0] = PricePoint(ts=ts, price=p)

    points = [buckets[k] for k in sorted(buckets.keys())]
    return PriceSeriesResponse(
        outcome_id=outcome_id,
        from_ts=from_ts_u,
        to_ts=to_ts_u,
        points=points,
    )


@router.get("/candles", response_model=CandleSeriesResponse, summary="K线（OHLCV）")
async def get_candles(
    outcome_id: int = Query(..., description="Outcome ID"),
    interval: Interval = Query("10s", description="K线周期，最小 10s"),
    from_ts: datetime = Query(..., description="起始时间（ISO）"),
    to_ts: datetime = Query(..., description="结束时间（ISO）"),
    fill: bool = Query(False, description="是否补齐空桶（无成交的桶用上一根 close 平铺）"),
    limit: int = Query(5000, ge=1, le=20000, description="最多返回K线根数（防止拉太多）"),
    max_trades: int = Query(200000, ge=1000, le=2000000, description="最多扫描交易条数（防止拖垮数据库）"),
    db: AsyncSession = Depends(get_async_session),
):
    """
    OHLCV K线：
    - 读该时间段内所有成交（Transaction）
    - 按 bucket 聚合出 o/h/l/c + v(成交量shares) + n(笔数)
    - 可选 fill：补齐无成交桶（更适合画连续K线）
    """
    if str(interval) not in _INTERVAL_SECONDS:
        raise HTTPException(status_code=400, detail="不支持的 interval")

    step = _INTERVAL_SECONDS[str(interval)]
    from_ts_u = _ensure_utc(from_ts)
    to_ts_u = _ensure_utc(to_ts)
    _validate_range(from_ts_u, to_ts_u)

    # 对齐桶边界，保证返回的K线起止规整
    aligned_from, aligned_to = _align_range_to_buckets(from_ts_u, to_ts_u, step)

    # 粗略防止一次拉太多桶
    # 统一采用 [aligned_from, aligned_to) 半开区间，避免桶边界重复。
    max_buckets = int((aligned_to.timestamp() - aligned_from.timestamp()) // step)
    if max_buckets > limit:
        raise HTTPException(
            status_code=400,
            detail=f"时间跨度过大：预计 {max_buckets} 根K线，超过 limit={limit}",
        )

    # 单次查询：fill 时向前多扩一个 bucket 以获取 previous close，省去第二次 DB 查询
    query_from = (
        datetime.fromtimestamp(int(aligned_from.timestamp()) - step, tz=timezone.utc)
        if fill else aligned_from
    )

    stmt = (
        select(
            Transaction.timestamp,
            Transaction.pre_market_price,
            Transaction.post_market_price,
            Transaction.price,
            Transaction.cost,
            Transaction.shares,
        )
        .where(
            and_(
                Transaction.outcome_id == outcome_id,
                Transaction.timestamp >= query_from,
                Transaction.timestamp < aligned_to,
            )
        )
        .order_by(Transaction.timestamp.asc())
        .limit(max_trades + 1)
    )
    res = await db.execute(stmt)
    rows = res.all()

    if len(rows) > max_trades:
        raise HTTPException(
            status_code=400,
            detail=f"该区间交易数超过 max_trades={max_trades}，请缩小时间范围或提高采样周期",
        )

    # bucket -> candle，同时从前导 bucket 提取 previous close
    buckets: Dict[datetime, Candle] = {}
    prev_close: Optional[float] = None

    for ts_raw, pre_mp, post_mp, price, cost, shares in rows:
        tx_open, tx_close = _extract_prices(
            pre_mp=float(pre_mp or 0.0),
            post_mp=float(post_mp or 0.0),
            price=float(price or 0.0),
            cost=float(cost or 0.0),
            shares=float(shares or 0.0),
        )
        if tx_close <= 0:
            continue

        ts = _ensure_utc(ts_raw)
        b0 = _bucket_start(ts, step)

        # 前导 bucket 的数据只用于提取 prev_close，不放入结果
        if b0 < aligned_from:
            prev_close = tx_close
            continue

        c = buckets.get(b0)
        if c is None:
            # 第一笔交易：open = 交易前价格，close = 交易后价格
            c = Candle(t=b0, o=tx_open, h=max(tx_open, tx_close), l=min(tx_open, tx_close), c=tx_close, v=0.0, n=0)
            buckets[b0] = c
        else:
            # 后续交易：只更新 high/low/close
            c.h = max(c.h, tx_open, tx_close)
            c.l = min(c.l, tx_open, tx_close)
            c.c = tx_close

        c.v += float(abs(shares or 0.0))
        c.n += 1

    # 不 fill：直接返回有成交的桶
    if not fill:
        candles = [buckets[k] for k in sorted(buckets.keys())]
        return CandleSeriesResponse(
            outcome_id=outcome_id,
            interval=interval,
            from_ts=from_ts_u,
            to_ts=to_ts_u,
            candles=candles,
        )

    # fill=True：补齐空桶（用上一根 close 平铺）
    candles: List[Candle] = []
    cur = aligned_from
    while cur < aligned_to:
        c = buckets.get(cur)
        if c is not None:
            candles.append(c)
            prev_close = c.c
        elif prev_close is not None:
            candles.append(Candle(t=cur, o=prev_close, h=prev_close, l=prev_close, c=prev_close, v=0.0, n=0))
        cur = datetime.fromtimestamp(int(cur.timestamp()) + step, tz=timezone.utc)

    return CandleSeriesResponse(
        outcome_id=outcome_id,
        interval=interval,
        from_ts=from_ts_u,
        to_ts=to_ts_u,
        candles=candles,
    )
