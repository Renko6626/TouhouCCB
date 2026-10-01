"""Backfill / rebuild the derived FX candle rows from raw ``FxTrade`` history.

This is an **offline maintenance** CLI: stop the backend first so no economic
producer can append trades while a pair is being rebuilt.  It never changes raw
trades, money, wallets or audit rows; it only replaces the derived
``fx_candle`` rows and rotates the pair's ``fx_market_data_state`` generation.

Usage::

    cd backend
    python -m scripts.backfill_fx_candles --pair-id 3 --dry-run
    python -m scripts.backfill_fx_candles --pair-id 3 --through-trade-id 12345 --yes
    python -m scripts.backfill_fx_candles --all --yes

Safety:

* uses the explicitly configured application database (``DATABASE_URL`` or the
  ``PG_*`` settings); it never embeds credentials and prints only the backend +
  database name;
* acquires the economic write ownership first and refuses to run when another
  process holds it (``OwnershipError``-style refusal, exit code 3);
* takes the per-pair exclusive FX GATE and then the ``fx_pair`` row lock, freezes
  the committed source cutoff ``max(fx_trade.id) <= --through-trade-id`` and
  rebuilds the derived rows in that same transaction;
* reads only the projected aggregation columns in pages, so a pair with a long
  trade history never materialises the full ORM row set.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import settings  # noqa: E402
from app.core.database import async_session_maker, engine  # noqa: E402
from app.models.fx import FxCandle, FxMarketDataState, FxPair, FxTrade  # noqa: E402
from app.services.credit.gates import GATES  # noqa: E402
from app.services.credit.keys import GroupKey  # noqa: E402
from app.services.credit.ownership import OWNERSHIP  # noqa: E402
from app.services.fx.candles import (  # noqa: E402
    apply_candle_batch,
    compute_fx_candle_rows,
    merge_row,
    new_history_version,
)

#: Only the columns the candle aggregation needs are projected.
_AGGREGATION_COLUMNS = (
    FxTrade.id,
    FxTrade.pair_id,
    FxTrade.created_at,
    FxTrade.post_price,
    FxTrade.side,
    FxTrade.input_amount,
    FxTrade.output_amount,
)

PAGE_SIZE = 1000


def _require_offline_runtime(pair_id: int) -> None:
    """Reject an in-process rebuild while this process owns the live runtime.

    ``rebuild_pair`` is an offline maintenance operation.  The production CLI
    additionally requires the economic write ownership, so it refuses while a
    live backend holds it.  This guard covers the remaining case: a caller that
    already started the in-process owner runtime, whose flusher concurrently
    locks the same state row and writes candles (state/FK lock deadlock risk).
    Readers (``write_owner=False``) are not rejected; their stale ring is
    already generation-checked by the read paths.
    """
    from app.services.fx.market_state import FX_MARKET_DATA
    if FX_MARKET_DATA.write_owner and FX_MARKET_DATA.started:
        raise RuntimeError(
            "rebuild_pair is offline maintenance: the in-process FX market-data "
            f"owner runtime is started (pair {pair_id}); stop the backend first"
        )


async def rebuild_pair(db: AsyncSession, pair_id: int,
                       through_trade_id: Optional[int] = None) -> int:
    """Replace one pair's derived candles from raw trades in the caller's txn.

    **Offline maintenance contract.**  The caller owns commit/rollback and the
    per-pair maintenance GATE, and must call this only when this process's FX
    market-data owner runtime is stopped (the production CLI refuses when
    another process holds the economic write ownership).  A live owner flusher
    concurrently locks the same ``FxMarketDataState`` row and writes candles, so
    an online rebuild can deadlock the state/FK locks; the guard below rejects
    that misuse instead of introducing a new global lock.

    ``through_trade_id`` freezes the source cutoff; ``None`` means "the pair's
    current committed max".  Under the ``fx_pair`` row lock this method:

    1. freezes ``max(FxTrade.id) <= through_trade_id`` (or the unbounded max);
    2. deletes the pair's ``FxCandle`` rows and resets its ``FxMarketDataState``
       cursor to 0 with a fresh ``history_version`` (so any in-flight flusher
       batch carrying the old generation is rejected by the storage layer);
    3. re-aggregates the projected trade pages and applies them through
       :func:`apply_candle_batch`, which writes the candle rows, the durable
       cursor and ``history_ready=True`` in the caller's transaction.

    Returns the effective cutoff actually rebuilt (0 when the pair has no
    trades).  Repeated rebuilds are exact: rows are replaced, never doubled.
    """
    _require_offline_runtime(pair_id)
    pair = (await db.execute(
        select(FxPair).where(FxPair.id == pair_id).with_for_update()
    )).scalars().first()
    if pair is None:
        raise ValueError(f"FX pair {pair_id} not found")

    cutoff_stmt = select(func.max(FxTrade.id)).where(FxTrade.pair_id == pair_id)
    if through_trade_id is not None:
        cutoff_stmt = cutoff_stmt.where(FxTrade.id <= int(through_trade_id))
    cutoff = (await db.execute(cutoff_stmt)).scalar()
    cutoff = int(cutoff or 0)

    # Derived rows are replaced, never merged: a rebuild must equal a fresh
    # full aggregation exactly.
    await db.execute(delete(FxCandle).where(FxCandle.pair_id == pair_id))

    state = (await db.execute(
        select(FxMarketDataState)
        .where(FxMarketDataState.pair_id == pair_id)
        .with_for_update()
    )).scalars().first()
    version = new_history_version()
    if state is not None:
        state.history_version = version
        state.last_trade_id = 0
        state.history_ready = False
        await db.flush()

    rows_by_key: dict[tuple[str, object], dict] = {}
    after = 0
    while True:
        page = (await db.execute(
            select(*_AGGREGATION_COLUMNS)
            .where(FxTrade.pair_id == pair_id, FxTrade.id > after, FxTrade.id <= cutoff)
            .order_by(FxTrade.id)
            .limit(PAGE_SIZE)
        )).all()
        if not page:
            break
        for row in compute_fx_candle_rows(page):
            key = (row["interval"], row["bucket_start"])
            existing = rows_by_key.get(key)
            rows_by_key[key] = merge_row(existing, row) if existing else row
        after = int(page[-1].id)

    await apply_candle_batch(
        db,
        pair_id=pair_id,
        expected_trade_id=0,
        through_trade_id=cutoff,
        rows=list(rows_by_key.values()),
        history_version=version,
        history_ready=True,
    )
    return cutoff


async def _select_pairs(pair_id: Optional[int]) -> list[int]:
    async with async_session_maker() as db:
        stmt = select(FxPair.id).order_by(FxPair.id)
        if pair_id is not None:
            stmt = stmt.where(FxPair.id == pair_id)
        return [int(value) for value in (await db.execute(stmt)).scalars().all()]


async def _trade_count(pair_id: int, through_trade_id: Optional[int]) -> int:
    async with async_session_maker() as db:
        stmt = select(func.count()).select_from(FxTrade).where(FxTrade.pair_id == pair_id)
        if through_trade_id is not None:
            stmt = stmt.where(FxTrade.id <= int(through_trade_id))
        return int((await db.execute(stmt)).scalar_one() or 0)


def _describe_database() -> str:
    url = make_url(settings.build_db_url())
    database = url.database or ""
    host = url.host or ""
    return f"{url.get_backend_name()}://{host}/{database}"


async def run(
    pair_ids: Sequence[int], through_trade_id: Optional[int], *, dry_run: bool,
) -> int:
    if not await OWNERSHIP.acquire():
        print(
            "拒绝执行：经济写所有权被其他进程持有 "
            f"({OWNERSHIP.reason})；请先停止后端再重建。"
        )
        return 3
    try:
        print(f"数据库：{_describe_database()}；目标 pair：{list(pair_ids)}")
        rebuilt = 0
        for pair_id in pair_ids:
            if dry_run:
                trades = await _trade_count(pair_id, through_trade_id)
                print(f"  pair={pair_id}: 将重建 {trades} 笔成交（dry-run，未写入）")
                rebuilt += trades
                continue
            # Per-pair maintenance GATE, then the pair row lock inside the txn.
            async with GATES.hold(exclusive=[GroupKey("fx", pair_id)]):
                async with async_session_maker() as db:
                    async with db.begin():
                        cutoff = await rebuild_pair(db, pair_id, through_trade_id)
                print(f"  pair={pair_id}: 已重建，截止 trade_id={cutoff}")
                rebuilt += 1
        print("dry-run 完成：未修改任何派生行" if dry_run else f"完成：重建 {rebuilt} 个 pair")
        return 0
    finally:
        await OWNERSHIP.release()


async def _main(args: argparse.Namespace) -> int:
    through = args.through_trade_id
    pair_ids = await _select_pairs(args.pair_id)
    if not pair_ids:
        print("没有匹配的 FX pair")
        return 0
    if args.dry_run:
        return await run(pair_ids, through, dry_run=True)
    if not args.yes:
        answer = input(
            f"将重建 {len(pair_ids)} 个 pair 的派生 K 线（原始成交/资金不变）。"
            "输入 REBUILD 继续: "
        ).strip()
        if answer != "REBUILD":
            print("已取消")
            return 1
    return await run(pair_ids, through, dry_run=False)


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair-id", type=int, help="只重建指定 FX pair")
    parser.add_argument("--all", action="store_true",
                        help="重建全部 pair（含 paused/archived 且保留历史的 pair）")
    parser.add_argument("--through-trade-id", type=int,
                        help="冻结的源成交截止 ID（缺省为该 pair 当前最大 id）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写")
    parser.add_argument("--yes", action="store_true", help="跳过 REBUILD 交互确认")
    args = parser.parse_args(argv)
    if args.pair_id is None and not args.all:
        parser.error("必须指定 --pair-id 或 --all")
    return args


async def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        return await _main(args)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
