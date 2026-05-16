"""Candle 物化表的写入逻辑。

被 buy/sell hot path 调用：在事务内同步 UPSERT N×4 行 candle。
也被 alembic migration / 兜底脚本 / startup hook 调用。

兼容 Postgres (生产) 和 SQLite (测试)：两者都支持 INSERT ... ON CONFLICT。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, List, Sequence

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import OutcomeCandle

# 跟 spec § 2 D1 锁定的 4 档；与 OutcomeCandle.interval 的 CheckConstraint 必须一致。
CANDLE_INTERVALS: List[tuple[str, int]] = [
    ("10s", 10),
    ("1m", 60),
    ("15m", 900),
    ("1h", 3600),
]


def _bucket_start(ts: datetime, step_seconds: int) -> datetime:
    """跟 chart.py:_bucket_start 同款实现（按 epoch 秒数 floor）。"""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % step_seconds), tz=timezone.utc)


def compute_candle_rows(
    traded_outcome_id: int,
    outcome_ids: Sequence[int],
    new_prices: Sequence[float],
    traded_shares: Decimal,
    ts: datetime,
) -> List[dict]:
    """为一笔 buy/sell 算出 N×4 行 UPSERT payload。

    - 每个 outcome × 每个 interval = 一行
    - 被直接交易的 outcome 行 volume_shares=traded_shares, n_trades=1
    - 联动行 volume_shares=0, n_trades=0
    - 首次 INSERT 时 O=H=L=C=该 outcome 的新价；后续 UPSERT 用 ON CONFLICT 合并。
    """
    assert len(outcome_ids) == len(new_prices), (
        f"outcome_ids ({len(outcome_ids)}) 与 new_prices ({len(new_prices)}) 长度必须一致"
    )

    rows: List[dict] = []
    for oid, price in zip(outcome_ids, new_prices):
        price_d = Decimal(str(price))
        is_traded = (oid == traded_outcome_id)
        v = traded_shares if is_traded else Decimal("0")
        n = 1 if is_traded else 0
        for interval, step in CANDLE_INTERVALS:
            bucket = _bucket_start(ts, step)
            rows.append({
                "outcome_id":   oid,
                "interval":     interval,
                "bucket_start": bucket,
                "open_price":   price_d,
                "high_price":   price_d,
                "low_price":    price_d,
                "close_price":  price_d,
                "volume_shares": v,
                "n_trades":      n,
            })
    return rows


async def upsert_candles(db: AsyncSession, rows: Iterable[dict]) -> None:
    """multi-row INSERT ... ON CONFLICT DO UPDATE 写入 candle 表。

    合并规则：
      - open_price 不在 set_ 中 → 首次 INSERT 的值永远保留
      - close_price 用 EXCLUDED（最后一次写入的值）
      - high_price = max(old, new)
      - low_price  = min(old, new)
      - volume_shares = old + new
      - n_trades = old + new
    """
    rows_list = list(rows)
    if not rows_list:
        return

    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        insert_fn = pg_insert
        greatest = func.greatest
        least = func.least
    elif dialect == "sqlite":
        insert_fn = sqlite_insert
        # SQLite 没 GREATEST/LEAST，用 MAX/MIN（标量上下文）
        greatest = func.max
        least = func.min
    else:
        raise NotImplementedError(f"不支持的 DB dialect: {dialect}")

    stmt = insert_fn(OutcomeCandle).values(rows_list)
    stmt = stmt.on_conflict_do_update(
        index_elements=["outcome_id", "interval", "bucket_start"],
        set_={
            "high_price":    greatest(OutcomeCandle.high_price, stmt.excluded.high_price),
            "low_price":     least(OutcomeCandle.low_price, stmt.excluded.low_price),
            "close_price":   stmt.excluded.close_price,
            "volume_shares": OutcomeCandle.volume_shares + stmt.excluded.volume_shares,
            "n_trades":      OutcomeCandle.n_trades + stmt.excluded.n_trades,
            "updated_at":    func.now(),
        },
    )
    await db.execute(stmt)
