"""PvE 注意力模型单测：作息窗口 / 下次看盘时间 / 行情推送三态。纯函数，无 DB。"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import random
from datetime import datetime, timedelta, timezone

from app.services.pve import attention
from app.services.pve.attention import ACTIVE_PRESETS, TZ


def _bj(hour: int, minute: int = 0) -> datetime:
    """北京时间 2026-08-28 hour:minute 的 UTC aware datetime。"""
    return datetime(2026, 8, 28, hour, minute, tzinfo=TZ).astimezone(timezone.utc)


# ── in_active_hours ─────────────────────────────────────────────────────


def test_active_hours_normal_window():
    windows = ACTIVE_PRESETS["evening"]  # 19:00–24:00
    assert attention.in_active_hours(20.0, windows)
    assert not attention.in_active_hours(12.0, windows)
    assert not attention.in_active_hours(3.0, windows)


def test_active_hours_overnight_wrap():
    windows = ACTIVE_PRESETS["owl"]  # 21:00–26:30（次日 2:30）
    assert attention.in_active_hours(23.0, windows)
    assert attention.in_active_hours(1.5, windows)   # 次日凌晨在窗口内
    assert not attention.in_active_hours(3.0, windows)
    assert not attention.in_active_hours(12.0, windows)


def test_active_hours_multi_window():
    windows = ACTIVE_PRESETS["worker"]  # 9:30–12:00 + 13:30–18:30
    assert attention.in_active_hours(10.0, windows)
    assert attention.in_active_hours(15.0, windows)
    assert not attention.in_active_hours(12.5, windows)  # 午休不看盘


# ── next_wake ───────────────────────────────────────────────────────────


def test_next_wake_inside_window_uses_jittered_interval():
    rng = random.Random(42)
    now = _bj(20, 0)  # 晚间窗口内
    params = {"check_interval_sec": 600, "active_preset": "evening", "hour_offset": 0.0}
    for _ in range(20):
        t = attention.next_wake(now, params, rng)
        delta = (t - now).total_seconds()
        # 0.6~1.6 抖动；均落在窗口内不再顺延
        assert 360 <= delta <= 960


def test_next_wake_outside_window_pushed_to_window():
    rng = random.Random(1)
    now = _bj(2, 0)  # 凌晨，evening 窗口外
    params = {"check_interval_sec": 600, "active_preset": "evening", "hour_offset": 0.0}
    for _ in range(10):
        t = attention.next_wake(now, params, rng)
        loc = t.astimezone(TZ)
        hour = loc.hour + loc.minute / 60
        assert hour >= 19.0, f"应顺延到 19:00 后，实际 {loc}"


def test_next_wake_always_never_pushes():
    rng = random.Random(7)
    now = _bj(3, 0)
    params = {"check_interval_sec": 120, "active_preset": "always", "hour_offset": 0.0}
    t = attention.next_wake(now, params, rng)
    assert (t - now).total_seconds() <= 120 * 1.6 + 1


# ── should_alert 三态 + 冷却 ────────────────────────────────────────────


def test_alert_below_threshold_is_none():
    rng = random.Random(0)
    now = datetime.now(timezone.utc)
    params = {"alert_threshold": 0.08, "alert_prob": 1.0}
    assert attention.should_alert(params, 0.03, now, None, rng) == "none"


def test_alert_above_threshold_wakes_with_prob_1():
    rng = random.Random(0)
    now = datetime.now(timezone.utc)
    params = {"alert_threshold": 0.05, "alert_prob": 1.0}
    assert attention.should_alert(params, 0.10, now, None, rng) == "wake"


def test_alert_prob_zero_is_ignored_not_none():
    """行情够大但没响应 → "ignored"（调用方要设冷却，同一波行情只 roll 一次）。"""
    rng = random.Random(0)
    now = datetime.now(timezone.utc)
    params = {"alert_threshold": 0.05, "alert_prob": 0.0}
    assert attention.should_alert(params, 0.10, now, None, rng) == "ignored"


def test_alert_cooldown_suppresses():
    rng = random.Random(0)
    now = datetime.now(timezone.utc)
    params = {"alert_threshold": 0.05, "alert_prob": 1.0}
    cooldown_until = now + timedelta(seconds=600)
    assert attention.should_alert(params, 0.10, now, cooldown_until, rng) == "none"


def test_alert_delay_within_range():
    rng = random.Random(3)
    params = {"alert_delay_min_sec": 60, "alert_delay_max_sec": 600}
    for _ in range(20):
        d = attention.alert_delay(params, rng).total_seconds()
        assert 60 <= d <= 600
