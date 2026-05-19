"""Anti-bot L4: 行为监控信号算法 + scheduler。

详见 docs/superpowers/specs/2026-05-20-anti-bot-design.md。

本文件分两部分：
1. 信号算法（pure functions，Task 9 + Task 10）
2. scheduler + 主循环（Task 11 + Task 12 实施）

每 30 min 扫近 2h Transaction，触发任一信号 → 写 bot_suspicion。
白名单 user 跳过扫描；6h 内同信号同 user 不重写。
"""
from __future__ import annotations
import statistics
from datetime import timezone
from typing import Iterable

# UTC+8 凌晨 03-06 = UTC 19-22 (前一天)
LATE_NIGHT_UTC_HOURS = {19, 20, 21}  # 19:00-22:00 UTC = 03:00-06:00 CST


def compute_high_freq_signal(transactions: Iterable, threshold: int) -> bool:
    """高频信号：窗口内 user 交易总笔数 ≥ threshold。"""
    return sum(1 for _ in transactions) >= threshold


def compute_late_night_signal(transactions: Iterable, threshold: int) -> bool:
    """凌晨信号：03-06 CST (= 19-22 UTC) 时段交易笔数 ≥ threshold。"""
    count = sum(
        1 for tx in transactions
        if tx.timestamp.astimezone(timezone.utc).hour in LATE_NIGHT_UTC_HOURS
    )
    return count >= threshold


def compute_regular_interval_signal(
    transactions: list, stddev_ms_threshold: int,
) -> bool:
    """间隔规律性信号：≥ 3 笔交易 + 相邻间隔的 stddev_ms < threshold。

    少于 3 笔不算（样本太小）。
    """
    if len(transactions) < 3:
        return False
    sorted_txns = sorted(transactions, key=lambda t: t.timestamp)
    intervals_ms = []
    for i in range(1, len(sorted_txns)):
        delta = sorted_txns[i].timestamp - sorted_txns[i - 1].timestamp
        intervals_ms.append(delta.total_seconds() * 1000)
    if len(intervals_ms) < 2:
        return False
    sd = statistics.stdev(intervals_ms)
    return sd < stddev_ms_threshold
