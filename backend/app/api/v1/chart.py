# app/api/v1/chart.py
#
# 图表数据 API：价格走势 + K 线。
#
# 核心设计：LMSR 交易任何选项会改变所有选项的价格。
# 因此图表不能只看目标 outcome 的 Transaction，
# 必须查整个 market 的所有交易，逐笔重放 shares 状态，
# 计算目标 outcome 在每笔交易后的瞬时价格。

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.models.base import Outcome, Transaction
from app.services.lmsr import get_current_price
from app.schemas.chart import (
    PricePoint,
    Candle,
    PriceSeriesResponse,
    CandleSeriesResponse,
    Interval,
)

router = APIRouter()

_INTERVAL_SECONDS: Dict[str, int] = {
    "10s": 10, "30s": 30, "1m": 60, "5m": 300,
    "15m": 900, "1h": 3600, "1d": 86400,
}


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _bucket_start(ts: datetime, step_seconds: int) -> datetime:
    ts = _ensure_utc(ts)
    epoch = int(ts.timestamp())
    bucket = epoch - (epoch % step_seconds)
    return datetime.fromtimestamp(bucket, tz=timezone.utc)


def _align_range_to_buckets(from_ts: datetime, to_ts: datetime, step: int) -> Tuple[datetime, datetime]:
    f = _bucket_start(from_ts, step)
    t0 = _ensure_utc(to_ts)
    to_epoch = int(t0.timestamp())
    if to_epoch % step != 0:
        to_epoch = to_epoch + (step - (to_epoch % step))
    t = datetime.fromtimestamp(to_epoch, tz=timezone.utc)
    return f, t


def _validate_range(from_ts_u: datetime, to_ts_u: datetime) -> None:
    if to_ts_u <= from_ts_u:
        raise HTTPException(status_code=400, detail="to_ts 必须大于 from_ts")


async def _get_market_context(outcome_id: int, db: AsyncSession):
    """获取 outcome 所属 market 的所有 outcomes 及 b 参数。"""
    outcome = await db.get(Outcome, outcome_id)
    if not outcome:
        raise HTTPException(status_code=404, detail="选项不存在")

    res = await db.execute(
        select(Outcome)
        .where(Outcome.market_id == outcome.market_id)
        .order_by(Outcome.id)
    )
    all_outcomes = res.scalars().all()

    from app.models.base import Market
    market = await db.get(Market, outcome.market_id)
    if not market:
        raise HTTPException(status_code=404, detail="市场不存在")

    target_idx = next((i for i, o in enumerate(all_outcomes) if o.id == outcome_id), None)
    if target_idx is None:
        raise HTTPException(status_code=400, detail="数据异常")

    # outcome_id → index 的映射
    oid_to_idx = {o.id: i for i, o in enumerate(all_outcomes)}
    all_outcome_ids = [o.id for o in all_outcomes]

    return market, all_outcomes, all_outcome_ids, oid_to_idx, target_idx, float(market.liquidity_b)


async def _replay_market_prices(
    db: AsyncSession,
    all_outcome_ids: List[int],
    oid_to_idx: Dict[int, int],
    target_idx: int,
    b: float,
    initial_shares: List[float],
    from_ts: datetime,
    to_ts: datetime,
    limit: int = 200000,
) -> List[Tuple[datetime, float, float]]:
    """
    查出市场内所有交易，逐笔重放 shares 状态，
    返回 [(timestamp, pre_price, post_price), ...] 目标 outcome 的价格序列。
    """
    stmt = (
        select(Transaction.timestamp, Transaction.outcome_id, Transaction.type, Transaction.shares)
        .where(
            and_(
                Transaction.outcome_id.in_(all_outcome_ids),
                Transaction.timestamp >= from_ts,
                Transaction.timestamp < to_ts,
            )
        )
        .order_by(Transaction.timestamp.asc())
        .limit(limit + 1)
    )
    res = await db.execute(stmt)
    rows = res.all()

    if len(rows) > limit:
        raise HTTPException(
            status_code=400,
            detail=f"该区间交易数超过 {limit}，请缩小时间范围或提高采样周期",
        )


    shares = list(initial_shares)
    points: List[Tuple[datetime, float, float]] = []

    for ts_raw, tx_outcome_id, tx_type, tx_shares in rows:
        idx = oid_to_idx.get(tx_outcome_id)
        if idx is None:
            continue

        ts = _ensure_utc(ts_raw)
        pre_price = get_current_price(shares, target_idx, b)

        amount = float(tx_shares)
        if tx_type in ("buy", "settle"):
            shares[idx] += amount
        elif tx_type in ("sell", "settle_lose"):
            shares[idx] -= amount

        post_price = get_current_price(shares, target_idx, b)
        points.append((ts, pre_price, post_price))

    return points


async def _get_initial_shares(
    db: AsyncSession,
    all_outcomes: list,
    all_outcome_ids: List[int],
    oid_to_idx: Dict[int, int],
    from_ts: datetime,
) -> List[float]:
    """
    计算 from_ts 之前的 shares 状态：
    从 Outcome.total_shares（当前值）减去 from_ts 之后的所有交易来反推。
    """
    current_shares = [float(o.total_shares) for o in all_outcomes]

    # 查 from_ts 之后的所有交易
    stmt = (
        select(Transaction.outcome_id, Transaction.type, Transaction.shares)
        .where(
            and_(
                Transaction.outcome_id.in_(all_outcome_ids),
                Transaction.timestamp >= from_ts,
            )
        )
        .order_by(Transaction.timestamp.asc())
    )
    res = await db.execute(stmt)

    # 逆向回退：当前 shares 减去之后的买入、加回之后的卖出
    for tx_oid, tx_type, tx_shares in res.all():
        idx = oid_to_idx.get(tx_oid)
        if idx is None:
            continue
        amount = float(tx_shares)
        if tx_type in ("buy", "settle"):
            current_shares[idx] -= amount
        elif tx_type in ("sell", "settle_lose"):
            current_shares[idx] += amount

    # 防止数据不一致导致负数（已结算市场的 settle_lose 未记录交易）
    return [max(0.0, s) for s in current_shares]


# ========================================
# 价格走势
# ========================================

@router.get("/price", response_model=PriceSeriesResponse, summary="价格曲线")
async def get_price_series(
    outcome_id: int = Query(..., description="Outcome ID"),
    from_ts: datetime = Query(..., description="起始时间（ISO）"),
    to_ts: datetime = Query(..., description="结束时间（ISO）"),
    limit: int = Query(5000, ge=1, le=20000),
    bucket: Optional[Interval] = Query(None),
    db: AsyncSession = Depends(get_async_session),
):
    from_ts_u = _ensure_utc(from_ts)
    to_ts_u = _ensure_utc(to_ts)
    _validate_range(from_ts_u, to_ts_u)

    market, all_outcomes, all_oids, oid_to_idx, target_idx, b = await _get_market_context(outcome_id, db)
    initial_shares = await _get_initial_shares(db, all_outcomes, all_oids, oid_to_idx, from_ts_u)
    price_points = await _replay_market_prices(
        db, all_oids, oid_to_idx, target_idx, b, initial_shares, from_ts_u, to_ts_u, limit,
    )

    if not bucket:
        points = [PricePoint(ts=ts, price=post) for ts, pre, post in price_points]
        return PriceSeriesResponse(outcome_id=outcome_id, from_ts=from_ts_u, to_ts=to_ts_u, points=points)

    # 按桶降采样
    step = _INTERVAL_SECONDS[str(bucket)]
    buckets: Dict[datetime, PricePoint] = {}
    for ts, pre, post in price_points:
        b0 = _bucket_start(ts, step)
        buckets[b0] = PricePoint(ts=ts, price=post)

    points = [buckets[k] for k in sorted(buckets.keys())]
    return PriceSeriesResponse(outcome_id=outcome_id, from_ts=from_ts_u, to_ts=to_ts_u, points=points)


# ========================================
# K 线
# ========================================

@router.get("/candles", response_model=CandleSeriesResponse, summary="K线（OHLCV）")
async def get_candles(
    outcome_id: int = Query(..., description="Outcome ID"),
    interval: Interval = Query("1m"),
    from_ts: datetime = Query(..., description="起始时间（ISO）"),
    to_ts: datetime = Query(..., description="结束时间（ISO）"),
    fill: bool = Query(False),
    limit: int = Query(5000, ge=1, le=20000),
    max_trades: int = Query(200000, ge=1000, le=2000000),
    db: AsyncSession = Depends(get_async_session),
):
    if str(interval) not in _INTERVAL_SECONDS:
        raise HTTPException(status_code=400, detail="不支持的 interval")

    step = _INTERVAL_SECONDS[str(interval)]
    from_ts_u = _ensure_utc(from_ts)
    to_ts_u = _ensure_utc(to_ts)
    _validate_range(from_ts_u, to_ts_u)

    aligned_from, aligned_to = _align_range_to_buckets(from_ts_u, to_ts_u, step)
    max_buckets = int((aligned_to.timestamp() - aligned_from.timestamp()) // step)
    if max_buckets > limit:
        raise HTTPException(status_code=400, detail=f"时间跨度过大：预计 {max_buckets} 根K线，超过 limit={limit}")

    # fill 时向前多扩一个 bucket 获取 prev_close
    query_from = (
        datetime.fromtimestamp(int(aligned_from.timestamp()) - step, tz=timezone.utc)
        if fill else aligned_from
    )

    market, all_outcomes, all_oids, oid_to_idx, target_idx, b = await _get_market_context(outcome_id, db)
    initial_shares = await _get_initial_shares(db, all_outcomes, all_oids, oid_to_idx, query_from)
    price_points = await _replay_market_prices(
        db, all_oids, oid_to_idx, target_idx, b, initial_shares, query_from, aligned_to, max_trades,
    )

    # 聚合成蜡烛
    candle_buckets: Dict[datetime, Candle] = {}
    prev_close: Optional[float] = None

    for ts, pre_price, post_price in price_points:
        b0 = _bucket_start(ts, step)

        if b0 < aligned_from:
            prev_close = post_price
            continue

        c = candle_buckets.get(b0)
        if c is None:
            c = Candle(
                t=b0,
                o=pre_price,
                h=max(pre_price, post_price),
                l=min(pre_price, post_price),
                c=post_price,
                v=0.0, n=0,
            )
            candle_buckets[b0] = c
        else:
            c.h = max(c.h, pre_price, post_price)
            c.l = min(c.l, pre_price, post_price)
            c.c = post_price
        c.n += 1

    if not fill:
        candles = [candle_buckets[k] for k in sorted(candle_buckets.keys())]
        return CandleSeriesResponse(outcome_id=outcome_id, interval=interval, from_ts=from_ts_u, to_ts=to_ts_u, candles=candles)

    # fill: 补齐空桶
    candles: List[Candle] = []
    cur = aligned_from
    while cur < aligned_to:
        c = candle_buckets.get(cur)
        if c is not None:
            candles.append(c)
            prev_close = c.c
        elif prev_close is not None:
            candles.append(Candle(t=cur, o=prev_close, h=prev_close, l=prev_close, c=prev_close, v=0.0, n=0))
        cur = datetime.fromtimestamp(int(cur.timestamp()) + step, tz=timezone.utc)

    return CandleSeriesResponse(outcome_id=outcome_id, interval=interval, from_ts=from_ts_u, to_ts=to_ts_u, candles=candles)
