"""注意力模型（spec §5.4）：看盘间隔 / 作息窗口 / 行情推送唤醒。全部纯函数。

作息按北京时间（用户群时区）计算；每个机器人有 hour_offset 个体偏移，
避免整点集体上线。alert（行情推送）无视作息与看盘间隔，带随机延迟与冷却。
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

# (start_hour, end_hour)，end > 24 表示跨夜（26.5 = 次日 02:30）
ACTIVE_PRESETS: dict[str, List[Tuple[float, float]]] = {
    "always": [(0.0, 24.0)],                    # 量化模板
    "worker": [(9.5, 12.0), (13.5, 18.5)],      # 上班摸鱼型
    "evening": [(19.0, 24.0)],                  # 晚间集中看盘
    "owl": [(21.0, 26.5)],                      # 夜猫子
    "loose": [(8.5, 25.0)],                     # 全天散漫型
}

ATTENTION_DEFAULTS = {
    "check_interval_sec": 1800,
    "active_preset": "loose",
    "hour_offset": 0.0,          # 个体作息偏移（小时）
    "alert_window_min": 10,      # 触发观察窗
    "alert_threshold": 0.08,     # 窗口内 |Δprice| 超过即算"大行情"
    "alert_prob": 0.5,           # 收到"推送"后实际响应的概率
    "alert_cooldown_sec": 1800,
    "alert_delay_min_sec": 60,   # 陆续点开通知的延迟区间
    "alert_delay_max_sec": 600,
}


def _local_hour(dt: datetime, hour_offset: float) -> float:
    loc = dt.astimezone(TZ)
    return (loc.hour + loc.minute / 60 + loc.second / 3600 - hour_offset) % 24


def in_active_hours(hour: float, windows: List[Tuple[float, float]]) -> bool:
    for start, end in windows:
        if end <= 24:
            if start <= hour < end:
                return True
        else:  # 跨夜窗口
            if hour >= start or hour < end - 24:
                return True
    return False


def _hours_until_window(hour: float, windows: List[Tuple[float, float]]) -> float:
    """距最近一个窗口开始的小时数（已在窗口内 → 0）。"""
    if in_active_hours(hour, windows):
        return 0.0
    return min((start - hour) % 24 for start, _ in windows)


# 全局活跃度（潮汐）边界：engine 每 tick 用 activity_step 演化一个乘子，
# 传给 next_wake 的 pace——活跃期全体看盘变勤、冷清期变懒，避免恒定到达率的机器味
ACTIVITY_MIN, ACTIVITY_MAX = 0.35, 1.8


def activity_step(a: float, amp: float, rng: random.Random) -> float:
    """OU 过程一步：向 1 回归 + 噪声。amp=0 → 恒 1（关闭潮汐）。
    tick=20s 时去相关时间约半小时——「今晚热闹、待会儿冷清」的慢波。"""
    if amp <= 0:
        return 1.0
    a += 0.01 * (1.0 - a) + amp * 0.025 * rng.gauss(0, 1)
    return min(ACTIVITY_MAX, max(ACTIVITY_MIN, a))


def next_wake(now: datetime, params: dict, rng: random.Random, pace: float = 1.0) -> datetime:
    """下次看盘时间 = now + 抖动后的看盘间隔（÷全局 pace），再推到作息窗口内。
    间隔带重尾：小概率沉迷刷盘（×0.15~0.35）/ 忙别的去了（×2~4）。"""
    base = float(params.get("check_interval_sec", ATTENTION_DEFAULTS["check_interval_sec"]))
    mult = rng.uniform(0.6, 1.6)
    roll = rng.random()
    if roll < 0.05:
        mult *= rng.uniform(0.15, 0.35)
    elif roll < 0.15:
        mult *= rng.uniform(2.0, 4.0)
    t = now + timedelta(seconds=base * mult / max(pace, 1e-6))
    windows = ACTIVE_PRESETS.get(
        params.get("active_preset", "loose"), ACTIVE_PRESETS["loose"]
    )
    off = float(params.get("hour_offset", 0.0))
    wait_h = _hours_until_window(_local_hour(t, off), windows)
    if wait_h > 0:
        t += timedelta(hours=wait_h + rng.uniform(0, 0.4))
    return t


def should_alert(
    params: dict,
    max_abs_change: float,
    now: datetime,
    cooldown_until: Optional[datetime],
    rng: random.Random,
) -> str:
    """是否被"大行情推送"炸出来。返回三态：
    "none"    没行情 / 冷却中（不设冷却）
    "ignored" 行情够大但这次没理（调用方仍须设冷却——同一波行情只 roll 一次）
    "wake"    响应，提前唤醒（调用方设冷却）
    """
    if cooldown_until is not None and now < cooldown_until:
        return "none"
    thr = float(params.get("alert_threshold", ATTENTION_DEFAULTS["alert_threshold"]))
    if max_abs_change < thr:
        return "none"
    if rng.random() < float(params.get("alert_prob", ATTENTION_DEFAULTS["alert_prob"])):
        return "wake"
    return "ignored"


def alert_delay(params: dict, rng: random.Random) -> timedelta:
    lo = float(params.get("alert_delay_min_sec", ATTENTION_DEFAULTS["alert_delay_min_sec"]))
    hi = float(params.get("alert_delay_max_sec", ATTENTION_DEFAULTS["alert_delay_max_sec"]))
    return timedelta(seconds=rng.uniform(lo, max(lo, hi)))
