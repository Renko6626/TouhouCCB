"""统一的用户称号体系。

全站只按 net_worth 一个口径定称号——排行榜（净值/消费）等服务端场景调
rank_title；/user/summary 把 RANK_THRESHOLDS 表下发给客户端本地映射
（spec 2026-08-21 § 6.4：net_worth 已下放前端算，rank 必须跟着下放，
否则服务端 rank 会和客户端显示的净值对不上）。阈值与文案改动只需改这一张表。

约定：net_worth 含义由调用方决定（个人/财富榜 net_worth = cash - debt
+ 持仓估值；消费榜 = 兑换消费总额 - 当前债务）。负值/0 都落到兜底档。
"""
from decimal import Decimal
from typing import Optional

# 降序阈值表；(None, ...) 是兜底档。判定规则：命中第一个
# 「thr is None 或 net_worth > thr」的条目（> 是严格大于）。
# 客户端（stores/user.ts::rankTitle）用同一规则本地映射。
RANK_THRESHOLDS: list[tuple[Optional[Decimal], str]] = [
    (Decimal("30000"), "ZUN"),
    (Decimal("10000"), "炒炒币大亨"),
    (Decimal("3000"), "妖怪操盘手"),
    (Decimal("1000"), "天狗交易员"),
    (Decimal("300"), "人里居民"),
    (None, "人类灵(已爆仓)"),
]


def rank_title(net_worth: Decimal) -> str:
    for thr, title in RANK_THRESHOLDS:
        if thr is None or net_worth > thr:
            return title
    return RANK_THRESHOLDS[-1][1]  # 不可达，防御
