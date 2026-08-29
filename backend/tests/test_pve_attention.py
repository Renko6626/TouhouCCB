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
        # 常规 0.6~1.6 抖动 + 偶发重尾（沉迷 ×0.15~0.35 / 失踪 ×2~4），全在此包络内
        assert 600 * 0.6 * 0.15 <= delta <= 600 * 1.6 * 4 + 1


def test_next_wake_has_heavy_tails():
    """间隔分布必须有重尾：偶尔很快回来刷盘、偶尔消失很久——
    否则全体机器人是匀速泊松流，一眼机器味。"""
    rng = random.Random(7)
    now = _bj(20, 0)
    params = {"check_interval_sec": 600, "active_preset": "always", "hour_offset": 0.0}
    deltas = [(attention.next_wake(now, params, rng) - now).total_seconds() for _ in range(500)]
    assert sum(1 for d in deltas if d < 600 * 0.4) > 5       # 沉迷刷盘出现过
    assert sum(1 for d in deltas if d > 600 * 1.8) > 10      # 长时间失踪出现过
    assert 600 * 0.7 <= sorted(deltas)[250] <= 600 * 1.5     # 中位数仍在常规区间


def test_next_wake_pace_scales_interval():
    """全局活跃度 pace：活跃期（pace>1）看盘更勤，冷清期（pace<1）更懒。"""
    now = _bj(20, 0)
    params = {"check_interval_sec": 600, "active_preset": "always", "hour_offset": 0.0}
    fast = [(attention.next_wake(now, params, random.Random(i), pace=2.0) - now).total_seconds()
            for i in range(100)]
    slow = [(attention.next_wake(now, params, random.Random(i), pace=0.5) - now).total_seconds()
            for i in range(100)]
    # 同种子逐对比较：pace=2 恰是 pace=0.5 的 1/4 间隔
    assert all(abs(f * 4 - s) < 1 for f, s in zip(fast, slow))


def test_activity_step_wave():
    """全局活跃度 OU 演化：amp=0 恒为 1（关闭）；有噪声时在界内波动且向 1 回归。"""
    rng = random.Random(3)
    assert attention.activity_step(0.5, 0.0, rng) == 1.0
    a = 1.0
    seen = []
    for _ in range(2000):
        a = attention.activity_step(a, 1.0, rng)
        seen.append(a)
        assert attention.ACTIVITY_MIN <= a <= attention.ACTIVITY_MAX
    assert max(seen) > 1.15 and min(seen) < 0.85    # 真的在波动
    # 从边界出发向 1 回归（无噪声分量看均值漂移）
    drift = attention.activity_step(attention.ACTIVITY_MAX, 0.0, rng)
    assert drift == 1.0  # amp=0 直接归位


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
