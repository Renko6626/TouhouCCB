"""图表接口性能基准测试

隔离 SQLite DB（/tmp/thccb_bench.db），种入合成数据，直接调
_get_initial_shares + _replay_market_prices，用 perf_counter 测耗时。

跑法：
    cd backend
    python -m scripts.bench_chart

会自动跑多组规模（小/中/大市场）× 多个时间窗（1h/24h/7d），
每组 5 次取 median + min/max。

注：本脚本针对 dev SQLite；生产 Postgres 表现会不同，仅作相对比较。
"""

import asyncio
import os
import random
import statistics
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import List

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

# 必须先设环境变量再 import app — bench 用独立 DB 文件避免污染 dev
BENCH_DB_PATH = "/tmp/thccb_bench.db"
if os.path.exists(BENCH_DB_PATH):
    os.remove(BENCH_DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{BENCH_DB_PATH}"

from app.models.base import Market, Outcome, Transaction, User  # noqa: E402
from app.api.v1.chart import (  # noqa: E402
    _fetch_initial_shares_and_replay,
    _get_market_context,
)


# ============================================================
# 数据生成
# ============================================================


async def seed_market(
    session: AsyncSession,
    n_outcomes: int,
    n_transactions: int,
    duration_hours: float,
    fill_market_prices_post: bool = True,
) -> tuple[int, int]:
    """种一个市场 + N outcomes + M transactions。

    返回 (market_id, target_outcome_id)。
    transactions 在 duration_hours 内均匀分布；type 50% buy / 50% sell；
    fill_market_prices_post=True 时填充快照（走 fast path），否则留 NULL（走 numpy）。
    """
    # User
    user = User(username=f"bench_{random.randint(0, 99999)}", cash=Decimal("1000000"))
    session.add(user)
    await session.flush()

    # Market
    b = 100.0
    market = Market(title=f"BenchMarket-{n_outcomes}o", liquidity_b=b)
    session.add(market)
    await session.flush()

    # Outcomes：起始 shares = 0
    outcomes = []
    for i in range(n_outcomes):
        o = Outcome(market_id=market.id, label=f"O{i}", total_shares=Decimal("0"))
        session.add(o)
        outcomes.append(o)
    await session.flush()
    outcome_ids = [o.id for o in outcomes]

    # 模拟 LMSR 状态演化生成 transactions
    current_shares = np.zeros(n_outcomes, dtype=np.float64)
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=duration_hours)

    txs: List[Transaction] = []
    for i in range(n_transactions):
        ts = start + timedelta(seconds=(duration_hours * 3600 * i / n_transactions))
        target = random.randrange(n_outcomes)
        tx_type = random.choice(["buy", "sell"])
        shares = random.uniform(1.0, 10.0)

        # 维护 shares 演化（保 sell 不超过当前持仓）
        if tx_type == "sell" and current_shares[target] < shares:
            tx_type = "buy"  # 不够卖就改买，简单兜底
        if tx_type == "buy":
            current_shares[target] += shares
        else:
            current_shares[target] -= shares

        # 计算 LMSR post 价快照
        if fill_market_prices_post:
            max_q = current_shares.max()
            exps = np.exp((current_shares - max_q) / b)
            prices = (exps / exps.sum()).tolist()
        else:
            prices = None

        tx = Transaction(
            user_id=user.id,
            outcome_id=outcome_ids[target],
            type=tx_type,
            shares=Decimal(str(shares)),
            cost=Decimal("0"),
            fee=Decimal("0"),
            gross=Decimal("0"),
            price=Decimal("0.5"),
            pre_market_price=Decimal("0.5"),
            post_market_price=Decimal("0.5"),
            timestamp=ts,
            market_prices_post=prices,
        )
        txs.append(tx)

    session.add_all(txs)
    await session.flush()

    # 更新 outcomes 的 total_shares 到最终值
    for i, o in enumerate(outcomes):
        o.total_shares = Decimal(str(current_shares[i]))
        session.add(o)
    await session.commit()

    return market.id, outcome_ids[0]


# ============================================================
# 基准
# ============================================================


async def bench_one(
    session_maker,
    target_outcome_id: int,
    lookback_hours: float,
    n_iter: int = 5,
) -> tuple[float, float, float]:
    """跑 n_iter 次 chart 链路（初始 shares + 重放），返回 (median_ms, min_ms, max_ms)。"""
    now = datetime.now(timezone.utc)
    from_ts = now - timedelta(hours=lookback_hours)
    to_ts = now

    times = []
    for _ in range(n_iter):
        async with session_maker() as session:
            t0 = time.perf_counter()
            _, all_outcomes, all_oids, oid_to_idx, target_idx, b = await _get_market_context(
                target_outcome_id, session
            )
            _, _ = await _fetch_initial_shares_and_replay(
                session, all_outcomes, all_oids, oid_to_idx, target_idx, b, from_ts, to_ts,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            times.append(elapsed_ms)

    return (statistics.median(times), min(times), max(times))


# ============================================================
# 主流程
# ============================================================


async def main() -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{BENCH_DB_PATH}",
        poolclass=NullPool,
    )

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # 种 3 组数据
    scenarios = [
        ("小市场", 3, 500, 24),      # 3 outcomes × 500 tx / 24h
        ("中市场", 5, 2000, 24),     # 5 outcomes × 2000 tx / 24h
        ("大市场", 10, 5000, 24),    # 10 outcomes × 5000 tx / 24h
        ("超大-7d", 10, 10000, 168),  # 10 outcomes × 10000 tx / 7d
    ]

    print(f"\n{'=' * 70}")
    print(f"图表性能基准 — SQLite ({BENCH_DB_PATH})")
    print(f"{'=' * 70}\n")

    results = []
    for name, n_outcomes, n_tx, dur_h in scenarios:
        async with session_maker() as session:
            mid, target_oid = await seed_market(session, n_outcomes, n_tx, dur_h)

        # 全量 fast path（snapshot 都已填）
        for lookback in [1, 6, 24, dur_h]:
            if lookback > dur_h:
                continue
            med, lo, hi = await bench_one(session_maker, target_oid, lookback)
            label = f"{name} ({n_outcomes}o × {n_tx} tx) — lookback {lookback}h"
            print(f"  {label:<52}  median {med:7.1f} ms  (min {lo:6.1f} / max {hi:6.1f})")
            results.append((label, med))

    print(f"\n{'-' * 70}")
    print(f"sum of medians: {sum(r[1] for r in results):.1f} ms")
    print(f"{'-' * 70}\n")

    await engine.dispose()


if __name__ == "__main__":
    random.seed(42)
    asyncio.run(main())
