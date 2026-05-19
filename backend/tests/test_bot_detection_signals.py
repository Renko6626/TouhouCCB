import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from datetime import datetime, timezone, timedelta

import pytest

from app.services.bot_detection import (
    compute_high_freq_signal,
    compute_late_night_signal,
    compute_regular_interval_signal,
)


def _txn(ts_offset_sec: float, cost: float = 10.0):
    """轻量"假交易"对象，只有 timestamp 和 cost。"""
    class T: pass
    t = T()
    t.timestamp = datetime.now(timezone.utc) - timedelta(seconds=ts_offset_sec)
    t.cost = cost
    return t


def test_high_freq_triggers_when_threshold_reached():
    # 130 笔交易，阈值 120 → 触发
    txns = [_txn(i) for i in range(130)]
    assert compute_high_freq_signal(txns, threshold=120) is True


def test_high_freq_not_triggered_below_threshold():
    txns = [_txn(i) for i in range(50)]
    assert compute_high_freq_signal(txns, threshold=120) is False


def test_late_night_signal():
    """凌晨 03-06 CST (UTC+8 → UTC 19-22) 时段的交易 ≥ 阈值 → 触发。"""
    now_utc = datetime.now(timezone.utc)
    # 构造 25 笔 UTC 19:00 的交易（北京时间 03:00）
    base = now_utc.replace(hour=19, minute=0, second=0, microsecond=0)
    late_txns = []
    for i in range(25):
        class T: pass
        t = T()
        t.timestamp = base + timedelta(minutes=i)
        t.cost = 10.0
        late_txns.append(t)
    # 加 10 笔白天的，不计
    day_base = now_utc.replace(hour=10, minute=0, second=0, microsecond=0)
    day_txns = []
    for i in range(10):
        class T: pass
        t = T()
        t.timestamp = day_base + timedelta(minutes=i)
        t.cost = 10.0
        day_txns.append(t)
    all_txns = late_txns + day_txns
    assert compute_late_night_signal(all_txns, threshold=20) is True


def test_late_night_not_triggered_below_threshold():
    now_utc = datetime.now(timezone.utc)
    base = now_utc.replace(hour=19, minute=0, second=0, microsecond=0)
    txns = []
    for i in range(5):
        class T: pass
        t = T()
        t.timestamp = base + timedelta(minutes=i)
        t.cost = 10.0
        txns.append(t)
    assert compute_late_night_signal(txns, threshold=20) is False


def test_regular_interval_triggers_when_stddev_low():
    """间隔几乎相同 (每次 1.0s) → stddev_ms 接近 0 → 触发。"""
    base = datetime.now(timezone.utc)
    txns = []
    for i in range(10):
        class T: pass
        t = T()
        t.timestamp = base + timedelta(seconds=i * 1.0)
        t.cost = 10.0
        txns.append(t)
    assert compute_regular_interval_signal(txns, stddev_ms_threshold=100) is True


def test_regular_interval_not_triggered_when_stddev_high():
    """间隔差异大 → 不触发。"""
    base = datetime.now(timezone.utc)
    intervals = [0.5, 3.7, 1.2, 8.4, 2.1, 6.9, 0.9, 5.5]
    txns = [_txn(0)]
    cumulative = 0.0
    for d in intervals:
        cumulative += d
        class T: pass
        t = T()
        t.timestamp = base + timedelta(seconds=cumulative)
        t.cost = 10.0
        txns.append(t)
    assert compute_regular_interval_signal(txns, stddev_ms_threshold=100) is False


def test_regular_interval_skipped_below_3_txns():
    """少于 3 笔 → 算法跳过，返 False。"""
    txns = [_txn(0), _txn(1)]
    assert compute_regular_interval_signal(txns, stddev_ms_threshold=100) is False
