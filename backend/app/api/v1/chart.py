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

import numpy as np
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.models.base import Outcome, Transaction
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


async def _fetch_initial_shares_and_replay(
    db: AsyncSession,
    all_outcomes: list,
    all_outcome_ids: List[int],
    oid_to_idx: Dict[int, int],
    target_idx: int,
    b: float,
    from_ts: datetime,
    to_ts: datetime,
    limit: int = 200000,
) -> Tuple[List[float], List[Tuple[datetime, float, float]]]:
    """
    单次 SELECT 拉 from_ts 之后的所有 Transaction（含 market_prices_post 快照），
    同时算出：
      - initial_shares：从当前 Outcome.total_shares 反向回退所有 from_ts 后的交易
      - price_points：from_ts..to_ts 区间内的逐笔价格曲线（fast path / numpy 兜底）

    合并前是两次 SELECT（_get_initial_shares + _replay_market_prices）覆盖大致重叠
    的区间，merge 后省一次 DB 往返 + 一次 SQL planning。to_ts < now 的历史窗口下
    会多拉一些 to_ts 之后的行（无害，仅用于 initial_shares 反向回退），代价 ~80B/row。
    """
    stmt = (
        select(
            Transaction.timestamp,
            Transaction.outcome_id,
            Transaction.type,
            Transaction.shares,
            Transaction.market_prices_post,
        )
        .where(
            and_(
                Transaction.outcome_id.in_(all_outcome_ids),
                Transaction.timestamp >= from_ts,
            )
        )
        .order_by(Transaction.timestamp.asc())
    )
    res = await db.execute(stmt)
    all_rows = res.all()

    # 1. initial_shares：从 current_shares 反向回退所有 from_ts 后的交易
    current_shares = [float(o.total_shares) for o in all_outcomes]
    for _ts, tx_oid, tx_type, tx_shares, _mpost in all_rows:
        idx = oid_to_idx.get(tx_oid)
        if idx is None:
            continue
        amount = float(tx_shares)
        if tx_type in ("buy", "settle"):
            current_shares[idx] -= amount
        elif tx_type in ("sell", "settle_lose"):
            current_shares[idx] += amount
    initial_shares = [max(0.0, s) for s in current_shares]

    # 2. 过滤 replay 子集：from_ts <= ts < to_ts（与原 _replay_market_prices 语义一致）
    replay_rows = [r for r in all_rows if _ensure_utc(r[0]) < to_ts]

    if len(replay_rows) > limit:
        raise HTTPException(
            status_code=400,
            detail=f"该区间交易数超过 {limit}，请缩小时间范围或提高采样周期",
        )

    if not replay_rows:
        return initial_shares, []

    n_outcomes = len(all_outcome_ids)
    all_have_snapshot = all(
        r[4] is not None and len(r[4]) == n_outcomes for r in replay_rows
    )

    if all_have_snapshot:
        price_points = _replay_from_snapshots(replay_rows, target_idx, initial_shares, b)
    else:
        price_points = _replay_numpy(replay_rows, all_outcome_ids, oid_to_idx, target_idx, b, initial_shares)

    return initial_shares, price_points


def _replay_from_snapshots(
    rows,
    target_idx: int,
    initial_shares: List[float],
    b: float,
) -> List[Tuple[datetime, float, float]]:
    """Fast path：直接读 market_prices_post 列。

    pre[k] = post[k-1]；首笔 pre 由 initial_shares 算一次起手价。
    """
    initial_arr = np.asarray(initial_shares, dtype=np.float64)
    max_q = initial_arr.max()
    exponents = np.exp((initial_arr - max_q) / b)
    prev_post = float(exponents[target_idx] / exponents.sum())

    points: List[Tuple[datetime, float, float]] = []
    for ts_raw, _oid, _type, _shares, snapshot in rows:
        ts = _ensure_utc(ts_raw)
        post = float(snapshot[target_idx])
        points.append((ts, prev_post, post))
        prev_post = post
    return points


def _replay_numpy(
    rows,
    all_outcome_ids: List[int],
    oid_to_idx: Dict[int, int],
    target_idx: int,
    b: float,
    initial_shares: List[float],
) -> List[Tuple[datetime, float, float]]:
    """Fallback：NumPy 矢量化重放（snapshot 任一 NULL 时整段走此路径）。"""
    n_outcomes = len(all_outcome_ids)
    indices: List[int] = []
    deltas_list: List[float] = []
    timestamps: List[datetime] = []
    for ts_raw, tx_outcome_id, tx_type, tx_shares, _snapshot in rows:
        idx = oid_to_idx.get(tx_outcome_id)
        if idx is None:
            continue
        amount = float(tx_shares)
        if tx_type in ("buy", "settle"):
            delta = amount
        elif tx_type in ("sell", "settle_lose"):
            delta = -amount
        else:
            continue
        indices.append(idx)
        deltas_list.append(delta)
        timestamps.append(_ensure_utc(ts_raw))

    if not indices:
        return []

    T = len(indices)
    deltas_matrix = np.zeros((T, n_outcomes), dtype=np.float64)
    deltas_matrix[np.arange(T), np.asarray(indices, dtype=np.int64)] = deltas_list

    shares_evolution = np.empty((T + 1, n_outcomes), dtype=np.float64)
    shares_evolution[0] = np.asarray(initial_shares, dtype=np.float64)
    np.cumsum(deltas_matrix, axis=0, out=shares_evolution[1:])
    shares_evolution[1:] += shares_evolution[0]

    max_q = shares_evolution.max(axis=1, keepdims=True)
    exponents = np.exp((shares_evolution - max_q) / b)
    target_prices = exponents[:, target_idx] / exponents.sum(axis=1)

    pre_prices = target_prices[:-1]
    post_prices = target_prices[1:]
    return [
        (timestamps[i], float(pre_prices[i]), float(post_prices[i]))
        for i in range(T)
    ]


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
    _, price_points = await _fetch_initial_shares_and_replay(
        db, all_outcomes, all_oids, oid_to_idx, target_idx, b, from_ts_u, to_ts_u, limit,
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
    _, price_points = await _fetch_initial_shares_and_replay(
        db, all_outcomes, all_oids, oid_to_idx, target_idx, b, query_from, aligned_to, max_trades,
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
