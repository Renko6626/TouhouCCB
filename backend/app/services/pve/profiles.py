"""生成时的活动短线参数与共享参数校验；人格决策仍由 believer 家族负责。"""
from __future__ import annotations

import math
import random

from app.services.pve.attention import ACTIVE_PRESETS, ATTENTION_DEFAULTS
from app.services.pve.templates import BelieverTemplate, FanTemplate, TEMPLATE_REGISTRY

# 推荐 50 人编制；API 返回给管理页，避免前后端各维护一份。
EVENT_MIX = {"chaser": 12, "sheep": 12, "swinger": 12, "bottom_fisher": 9, "fan": 5}


def event_params(params: dict, rng: random.Random, template: str) -> dict:
    """保留信念/从众/动量人格差异，缩短持仓周期并增大有信号时的下注。"""
    return {
        **params,
        "activity_mode": "event",
        "anchor_adapt_rate": 0.0 if issubclass(TEMPLATE_REGISTRY[template], FanTemplate) else 0.12,
        "shock_prob": 0.0 if issubclass(TEMPLATE_REGISTRY[template], FanTemplate) else params["shock_prob"],
        "anchor_adapt_window_min": 15,
        "anchor_adapt_min_traders": 3,
        "anchor_adapt_min_shift": 0.015,
        "anchor_adapt_wait_sec": 600,
        "active_preset": "always",
        "check_interval_sec": 240,
        "hour_offset": 0.0,
        "lookback_min": round(rng.uniform(5, 10), 2),
        "act_threshold": round(rng.uniform(0.006, 0.012), 4),
        "edge_scale": round(rng.uniform(0.02, 0.035), 4),
        "aggressiveness": round(rng.uniform(0.5, 0.8), 4),
        "max_bet_frac": round(rng.uniform(0.6, 0.85), 4),
        "skip_prob": round(rng.uniform(0.05, 0.12), 4),
        "take_profit": round(rng.uniform(0.04, 0.09), 4),
        "stop_loss": round(rng.uniform(0.06, 0.12), 4),
        "cash_reserve_cny": round(rng.uniform(5, 15), 2),
        "yolo_prob": round(rng.uniform(0.08, 0.15), 4),
        "alert_threshold": round(rng.uniform(0.015, 0.04), 4),
        "alert_prob": round(rng.uniform(0.4, 0.75), 4),
        "alert_cooldown_sec": rng.randint(600, 1200),
        "alert_delay_min_sec": 30,
        "alert_delay_max_sec": 120,
    }


def validate_params(template: str, params: dict) -> None:
    """验证合并后的参数，保护账户池免受单个坏参数影响。"""
    cls = TEMPLATE_REGISTRY.get(template)
    if cls is None:
        raise ValueError(f"未知模板：{template}")
    merged = {**ATTENTION_DEFAULTS, **cls.default_params, **params}
    mode = merged["activity_mode"]
    if mode not in ("longterm", "event"):
        raise ValueError("activity_mode 需为 longterm/event")
    if mode == "event" and not issubclass(cls, BelieverTemplate):
        raise ValueError("活动短线模式只支持信念散户家族")
    if not isinstance(merged["active_preset"], str) or merged["active_preset"] not in ACTIVE_PRESETS:
        raise ValueError("未知作息模板 active_preset")
    if "herd_signal" in merged and merged["herd_signal"] not in ("price", "flow"):
        raise ValueError("herd_signal 需为 price/flow")
    defaults = {**ATTENTION_DEFAULTS, **cls.default_params}
    positive = {"check_interval_sec", "lookback_min", "edge_scale", "flow_scale", "scale_price", "levels",
                "anchor_adapt_window_min", "anchor_adapt_min_traders"}
    signed = {"hour_offset", "herd_coef", "trend_coef"}
    for key, default in defaults.items():
        value = merged[key]
        if isinstance(default, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} 需为有限数值")
            if key in positive and value <= 0:
                raise ValueError(f"{key} 需大于 0")
            if key not in signed and value < 0:
                raise ValueError(f"{key} 不可为负数")
            # 旧生成器可能把 w_swing 扰动到 >1；decide 本来就钳回 [0,1]，仍兼容这些账户。
            if (key.endswith("_prob") or key in ("max_bet_frac", "anchor_adapt_rate")) and value > 1:
                raise ValueError(f"{key} 需在 0–1 之间")
    for key in ("outcome_id", "price_low", "price_high"):
        value = merged.get(key)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} 需为空或正数")
            if key == "outcome_id" and not isinstance(value, int):
                raise ValueError("outcome_id 需为整数")
            if key != "outcome_id" and value >= 1:
                raise ValueError(f"{key} 需小于 1")
