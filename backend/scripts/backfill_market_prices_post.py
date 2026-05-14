"""
回填 Transaction.market_prices_post：扫所有市场，按时间顺序重放 LMSR，把每笔
交易（buy/sell）的全市场 post 价 list[float] 填进 market_prices_post 列。

幂等：只 UPDATE market_prices_post IS NULL 的行。中断后再跑会从未填的市场/行继续。

settle / settle_lose 留 NULL（chart 接口看到 NULL 行整段退回 NumPy 重放）。

运行：
    cd backend
    python -m scripts.backfill_market_prices_post                # 全量回填
    python -m scripts.backfill_market_prices_post --dry-run      # 只统计不写
    python -m scripts.backfill_market_prices_post --market-id 23 # 单市场
    python -m scripts.backfill_market_prices_post --batch 500    # 自定义批大小
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import List

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import engine  # noqa: E402
from app.models.base import Market, Outcome, Transaction  # noqa: E402


def _vectorized_replay_snapshots(
    rows: List[tuple],
    oid_to_idx: dict,
    n_outcomes: int,
    b: float,
) -> tuple[List[int], np.ndarray]:
    """
    给定一个市场的全部 (tx_id, outcome_id, type, shares) 行（按 timestamp 升序），
    返回 (tx_ids, snapshots) where snapshots[k] = 第 k 笔交易后的全市场 post 价 (N,)。

    initial_shares 固定 [0, 0, ..., 0]（市场创建时各 outcome.total_shares 默认 0）。
    settle_lose 当 sell 处理（减 shares），跟 chart._replay_numpy 一致。
    """
    indices: List[int] = []
    deltas_list: List[float] = []
    tx_ids: List[int] = []
    for tx_id, tx_oid, tx_type, tx_shares in rows:
        idx = oid_to_idx.get(tx_oid)
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
        tx_ids.append(int(tx_id))

    if not indices:
        return [], np.empty((0, n_outcomes), dtype=np.float64)

    T = len(indices)
    deltas_matrix = np.zeros((T, n_outcomes), dtype=np.float64)
    deltas_matrix[np.arange(T), np.asarray(indices, dtype=np.int64)] = deltas_list

    shares_evolution = np.empty((T + 1, n_outcomes), dtype=np.float64)
    shares_evolution[0] = 0.0
    np.cumsum(deltas_matrix, axis=0, out=shares_evolution[1:])
    # 第 k 笔 post 状态 = shares_evolution[k+1]
    post_states = shares_evolution[1:]

    max_q = post_states.max(axis=1, keepdims=True)
    exponents = np.exp((post_states - max_q) / b)
    snapshots = exponents / exponents.sum(axis=1, keepdims=True)
    return tx_ids, snapshots


async def backfill_market(
    db: AsyncSession,
    market_id: int,
    batch: int,
    dry_run: bool,
) -> dict:
    """回填单个市场。返回统计 dict。"""
    market = await db.get(Market, market_id)
    if not market:
        return {"market_id": market_id, "skipped": "市场不存在"}

    outs_res = await db.execute(
        select(Outcome).where(Outcome.market_id == market_id).order_by(Outcome.id)
    )
    outcomes = outs_res.scalars().all()
    if not outcomes:
        return {"market_id": market_id, "skipped": "无 outcome"}

    oid_to_idx = {o.id: i for i, o in enumerate(outcomes)}
    n_outcomes = len(outcomes)
    b = float(market.liquidity_b)

    # 拉该市场全部交易（按时间升序）— 含已填 snapshot 的，因为重放需要完整序列
    outcome_ids = [o.id for o in outcomes]
    tx_res = await db.execute(
        select(
            Transaction.id,
            Transaction.outcome_id,
            Transaction.type,
            Transaction.shares,
            Transaction.market_prices_post,
        )
        .where(Transaction.outcome_id.in_(outcome_ids))
        .order_by(Transaction.timestamp.asc())
    )
    rows = tx_res.all()
    if not rows:
        return {"market_id": market_id, "total": 0, "filled": 0, "skipped_existing": 0}

    # 重放
    replay_rows = [(r[0], r[1], r[2], r[3]) for r in rows]
    tx_ids, snapshots = _vectorized_replay_snapshots(replay_rows, oid_to_idx, n_outcomes, b)

    # 只填 buy/sell（settle/settle_lose 留 NULL）+ 只填原本 NULL 的行
    existing_state = {r[0]: (r[2], r[4]) for r in rows}  # tx_id -> (type, current_snapshot)

    to_update: List[tuple[int, str]] = []
    skipped_existing = 0
    skipped_type = 0
    for k, tx_id in enumerate(tx_ids):
        tx_type, current = existing_state[tx_id]
        if tx_type not in ("buy", "sell"):
            skipped_type += 1
            continue
        if current is not None and len(current) == n_outcomes:
            skipped_existing += 1
            continue
        snapshot_list = snapshots[k].tolist()
        to_update.append((tx_id, json.dumps(snapshot_list)))

    filled = 0
    if to_update and not dry_run:
        # batch UPDATE：分批跑，避免单条事务太长
        for i in range(0, len(to_update), batch):
            chunk = to_update[i : i + batch]
            # 用参数化的 UPDATE 一条条跑（PG/SQLite 都支持），代价可接受（回填一次性）
            for tx_id, snap_json in chunk:
                await db.execute(
                    text(
                        'UPDATE "transaction" SET market_prices_post = :snap WHERE id = :tx_id'
                    ),
                    {"snap": snap_json, "tx_id": tx_id},
                )
            await db.commit()
            filled += len(chunk)

    return {
        "market_id": market_id,
        "total_trades": len(rows),
        "candidates_buy_sell": len(tx_ids) - skipped_type,
        "skipped_settle_like": skipped_type,
        "skipped_already_filled": skipped_existing,
        "to_fill": len(to_update),
        "filled": filled if not dry_run else 0,
    }


async def run(args: argparse.Namespace) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with session_factory() as db:
        if args.market_id is not None:
            market_ids = [int(args.market_id)]
        else:
            # 全部市场
            mres = await db.execute(select(Market.id).order_by(Market.id))
            market_ids = [int(m) for m in mres.scalars().all()]

    print(f"[backfill] 目标市场数：{len(market_ids)}  batch={args.batch}  dry_run={args.dry_run}")

    overall_filled = 0
    overall_to_fill = 0
    t0 = time.perf_counter()
    for mid in market_ids:
        async with session_factory() as db:
            try:
                stats = await backfill_market(db, mid, args.batch, args.dry_run)
            except Exception as e:
                print(f"[backfill] market={mid} ERROR: {e!r}")
                continue
        print(f"[backfill] market={mid}  {stats}")
        overall_filled += stats.get("filled", 0)
        overall_to_fill += stats.get("to_fill", 0)

    elapsed = time.perf_counter() - t0
    print(
        f"\n[backfill] 完成。耗时 {elapsed:.1f}s  待回填总数={overall_to_fill}  "
        f"实际写入={overall_filled}  {'(DRY-RUN)' if args.dry_run else ''}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--market-id", type=int, default=None, help="只跑单个 market（调试用）")
    parser.add_argument("--batch", type=int, default=1000, help="UPDATE 提交批大小")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
