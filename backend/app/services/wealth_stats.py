"""平台净值分布的统计函数（pure math，与 DB 解耦便于单测）。

`compute_wealth_distribution()` 接受一个 net_worth 浮点数列表 + 整体汇总
（total_cash / debt / holdings）输出包含均值、方差、分位数、基尼系数、按
称号阈值分桶的完整统计字典。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, List


# 与 app/services/rank.py 的阈值一一对应。改 rank 阈值时这里也要同步改。
_BRACKETS: List[tuple[str, float, float]] = [
    # (label, lower_exclusive, upper_inclusive)；lower=-inf 用 -math.inf，upper=inf 用 math.inf
    ("人类灵(已爆仓)", -math.inf, 300.0),
    ("人里居民",       300.0,     1000.0),
    ("天狗交易员",     1000.0,    3000.0),
    ("妖怪操盘手",     3000.0,    10000.0),
    ("炒炒币大亨",     10000.0,   30000.0),
    ("ZUN",            30000.0,   math.inf),
]


@dataclass
class WealthBracket:
    label: str
    lower: float | None      # null 表示 -inf
    upper: float | None      # null 表示 +inf
    count: int
    sum_wealth: float
    pct: float               # 占用户总数百分比


def _percentile(sorted_values: List[float], p: float) -> float:
    """线性插值百分位数。p ∈ [0, 100]。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * (p / 100.0)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_values[int(rank)]
    frac = rank - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def gini_coefficient(values: List[float]) -> float:
    """基尼系数。0=完全平等，1=完全不平等。
    支持含负值的输入：把所有值整体平移至非负后再算（仅影响绝对水平，不影响相对差异结构）。
    全部为 0 / 空列表 返回 0。
    """
    if not values:
        return 0.0
    vs = sorted(values)
    n = len(vs)
    if vs[0] < 0:
        offset = -vs[0]
        vs = [v + offset for v in vs]
    total = sum(vs)
    if total <= 0:
        return 0.0
    weighted = 0.0
    for i, v in enumerate(vs, start=1):
        weighted += i * v
    g = (2 * weighted / total - (n + 1)) / n
    return max(0.0, min(1.0, g))


def _bucket(values: List[float], n_users: int) -> List[WealthBracket]:
    out: List[WealthBracket] = []
    for label, lo, hi in _BRACKETS:
        # 区间约定：lo 不含、hi 含；最低档 lo=-inf 等价于"无下限"
        in_bracket = [v for v in values if lo < v <= hi]
        out.append(WealthBracket(
            label=label,
            lower=None if math.isinf(lo) else lo,
            upper=None if math.isinf(hi) else hi,
            count=len(in_bracket),
            sum_wealth=round(sum(in_bracket), 2),
            pct=round(len(in_bracket) / n_users * 100, 2) if n_users else 0.0,
        ))
    return out


def compute_wealth_distribution(
    net_worths: List[float],
    *,
    total_cash: float,
    total_debt: float,
    total_holdings_value: float,
) -> Dict[str, Any]:
    """汇总输入并返回 JSON-ready dict。"""
    n = len(net_worths)
    if n == 0:
        return {
            "user_count": 0,
            "total_cash": round(total_cash, 2),
            "total_debt": round(total_debt, 2),
            "total_holdings_value": round(total_holdings_value, 2),
            "total_net_worth": 0.0,
            "mean": 0.0,
            "median": 0.0,
            "std": 0.0,
            "min": 0.0,
            "max": 0.0,
            "p10": 0.0, "p25": 0.0, "p50": 0.0,
            "p75": 0.0, "p90": 0.0, "p99": 0.0,
            "gini": 0.0,
            "brackets": [asdict(b) for b in _bucket([], 0)],
        }

    sorted_nw = sorted(net_worths)
    total = sum(net_worths)
    mean = total / n
    var = sum((v - mean) ** 2 for v in net_worths) / n
    std = math.sqrt(var)
    return {
        "user_count": n,
        "total_cash": round(total_cash, 2),
        "total_debt": round(total_debt, 2),
        "total_holdings_value": round(total_holdings_value, 2),
        "total_net_worth": round(total, 2),
        "mean": round(mean, 2),
        "median": round(_percentile(sorted_nw, 50), 2),
        "std": round(std, 2),
        "min": round(sorted_nw[0], 2),
        "max": round(sorted_nw[-1], 2),
        "p10": round(_percentile(sorted_nw, 10), 2),
        "p25": round(_percentile(sorted_nw, 25), 2),
        "p50": round(_percentile(sorted_nw, 50), 2),
        "p75": round(_percentile(sorted_nw, 75), 2),
        "p90": round(_percentile(sorted_nw, 90), 2),
        "p99": round(_percentile(sorted_nw, 99), 2),
        "gini": round(gini_coefficient(net_worths), 4),
        "brackets": [asdict(b) for b in _bucket(net_worths, n)],
    }
