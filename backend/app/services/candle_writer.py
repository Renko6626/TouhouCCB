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
    if len(outcome_ids) != len(new_prices):
        raise ValueError(
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
                "updated_at":    ts,  # 显式填，避免 SQLAlchemy core insert() 跳过 SQLModel default_factory
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


async def backfill_one_market(db: AsyncSession, market_id: int) -> int:
    """回填某个 market 的全部 buy/sell Transaction 到 candle 表。

    幂等：通过 upsert_candles 的 ON CONFLICT DO UPDATE 合并；但**不能对同一
    Transaction 重复调用**（否则 volume/n 会被累加）—— 调用方应先
    `DELETE FROM outcome_candle WHERE outcome_id IN (...)` 或保证只调一次。

    返回处理的 Transaction 行数。
    """
    from sqlalchemy import select
    from app.models.base import Market, Outcome, Transaction, TransactionType

    market = await db.get(Market, market_id)
    if market is None:
        return 0
    outcomes = (await db.execute(
        select(Outcome).where(Outcome.market_id == market_id).order_by(Outcome.id)
    )).scalars().all()
    if not outcomes:
        return 0
    outcome_ids = [o.id for o in outcomes]

    txs = (await db.execute(
        select(Transaction)
        .where(
            Transaction.outcome_id.in_(outcome_ids),
            Transaction.type.in_([TransactionType.BUY, TransactionType.SELL]),
        )
        .order_by(Transaction.timestamp.asc())
    )).scalars().all()

    n_processed = 0
    for tx in txs:
        # 优先用 market_prices_post 快照；缺失时 fallback
        if tx.market_prices_post and len(tx.market_prices_post) == len(outcome_ids):
            new_prices = [float(p) for p in tx.market_prices_post]
        else:
            # fallback: 用 tx.post_market_price 当被交易 outcome 价，其他 outcome 用 1/N
            # 这是退化路径，生产前应先跑 backfill_market_prices_post 保证 snapshot 齐全
            new_prices = [
                float(tx.post_market_price) if oid == tx.outcome_id
                else 1.0 / len(outcome_ids) for oid in outcome_ids
            ]
        rows = compute_candle_rows(
            traded_outcome_id=tx.outcome_id,
            outcome_ids=outcome_ids,
            new_prices=new_prices,
            traded_shares=tx.shares,
            ts=tx.timestamp,
        )
        await upsert_candles(db, rows)
        n_processed += 1

    return n_processed
