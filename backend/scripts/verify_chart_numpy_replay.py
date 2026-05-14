"""
对照验证：chart._replay_market_prices 的老 Python-loop 实现 vs 新 NumPy 矢量化实现。

合成 (N outcomes, T transactions) 的随机交易序列，分别跑两套实现，
逐点比对 pre_price / post_price 误差，并测速。

通过条件：max_abs_err < 1e-10
"""
from __future__ import annotations

import math
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

import numpy as np


# ─── 老实现：直接照搬 chart.py 改动前的逻辑 ──────────────────────────────

def _old_get_current_price(shares_list: List[float], target_index: int, b: float) -> float:
    max_q = max(shares_list)
    exponents = [math.exp((q - max_q) / b) for q in shares_list]
    return exponents[target_index] / sum(exponents)


def old_replay(
    rows: List[Tuple[datetime, int, str, float]],
    oid_to_idx: Dict[int, int],
    target_idx: int,
    b: float,
    initial_shares: List[float],
) -> List[Tuple[datetime, float, float]]:
    shares = list(initial_shares)
    points: List[Tuple[datetime, float, float]] = []
    for ts, tx_outcome_id, tx_type, tx_shares in rows:
        idx = oid_to_idx.get(tx_outcome_id)
        if idx is None:
            continue
        pre_price = _old_get_current_price(shares, target_idx, b)
        amount = float(tx_shares)
        if tx_type in ("buy", "settle"):
            shares[idx] += amount
        elif tx_type in ("sell", "settle_lose"):
            shares[idx] -= amount
        post_price = _old_get_current_price(shares, target_idx, b)
        points.append((ts, pre_price, post_price))
    return points


# ─── 新实现：镜像 chart.py 修改后的 NumPy 版 ──────────────────────────────

def new_replay(
    rows: List[Tuple[datetime, int, str, float]],
    oid_to_idx: Dict[int, int],
    target_idx: int,
    b: float,
    initial_shares: List[float],
    n_outcomes: int,
) -> List[Tuple[datetime, float, float]]:
    indices: List[int] = []
    deltas_list: List[float] = []
    timestamps: List[datetime] = []
    for ts, tx_outcome_id, tx_type, tx_shares in rows:
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
        timestamps.append(ts)

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


# ─── 合成数据 ─────────────────────────────────────────────────────────────

def synth_rows(
    n_outcomes: int,
    n_trades: int,
    b: float,
    seed: int = 42,
) -> Tuple[
    List[Tuple[datetime, int, str, float]],
    Dict[int, int],
    List[float],
    int,
]:
    rng = random.Random(seed)
    outcome_ids = [1000 + i for i in range(n_outcomes)]
    oid_to_idx = {oid: i for i, oid in enumerate(outcome_ids)}
    target_idx = rng.randrange(n_outcomes)

    initial_shares = [rng.uniform(0.0, b * 0.5) for _ in range(n_outcomes)]

    base_ts = datetime(2026, 5, 1, tzinfo=timezone.utc)
    rows: List[Tuple[datetime, int, str, float]] = []
    for k in range(n_trades):
        oid = rng.choice(outcome_ids)
        tx_type = rng.choice(["buy", "buy", "buy", "sell", "settle", "settle_lose"])
        # 卖出时不能超过当前持仓 — 但这里只是合成"市场内全体交易"，不在意单用户约束。
        # 为了避免极端负 shares 把 exp 推到 overflow，控制单笔幅度
        amount = rng.uniform(0.01, b * 0.01)
        ts = base_ts + timedelta(seconds=k)
        rows.append((ts, oid, tx_type, amount))

    return rows, oid_to_idx, initial_shares, target_idx


# ─── 主流程 ───────────────────────────────────────────────────────────────

def run_case(n_outcomes: int, n_trades: int, b: float, seed: int) -> None:
    label = f"N={n_outcomes:>2} T={n_trades:>7} b={b:>7.1f} seed={seed}"
    rows, oid_to_idx, initial_shares, target_idx = synth_rows(n_outcomes, n_trades, b, seed)

    t0 = time.perf_counter()
    old_points = old_replay(rows, oid_to_idx, target_idx, b, initial_shares)
    t_old = time.perf_counter() - t0

    t0 = time.perf_counter()
    new_points = new_replay(rows, oid_to_idx, target_idx, b, initial_shares, n_outcomes)
    t_new = time.perf_counter() - t0

    assert len(old_points) == len(new_points), f"长度不匹配: {len(old_points)} vs {len(new_points)}"

    max_err_pre = 0.0
    max_err_post = 0.0
    for (ts_o, pre_o, post_o), (ts_n, pre_n, post_n) in zip(old_points, new_points):
        assert ts_o == ts_n, f"timestamp 不匹配: {ts_o} vs {ts_n}"
        max_err_pre = max(max_err_pre, abs(pre_o - pre_n))
        max_err_post = max(max_err_post, abs(post_o - post_n))
    max_err = max(max_err_pre, max_err_post)

    speedup = t_old / t_new if t_new > 0 else float("inf")
    status = "OK" if max_err < 1e-10 else "FAIL"
    print(
        f"[{status}] {label} | n_points={len(old_points):>7} "
        f"| max_err={max_err:.3e} | old={t_old*1000:>8.1f}ms "
        f"new={t_new*1000:>7.1f}ms speedup={speedup:>5.1f}x"
    )

    if max_err >= 1e-10:
        raise SystemExit(f"数值偏差超阈值: {max_err}")


def main() -> None:
    print("== chart._replay_market_prices: old vs new (NumPy 矢量化) 数值/性能对照 ==\n")

    # 覆盖 N=1 边界、典型 2 元、多选；T 量级覆盖小/中/大
    cases = [
        (1, 100, 1000.0, 1),
        (2, 100, 1000.0, 2),
        (2, 10000, 1000.0, 3),
        (5, 10000, 5000.0, 4),
        (2, 100000, 10000.0, 5),
        (5, 100000, 10000.0, 6),
    ]
    for n_out, n_tr, b, seed in cases:
        run_case(n_out, n_tr, b, seed)

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
