"""统一的用户称号体系。

全站只按 net_worth 一个口径定称号——无论是个人 summary、排行榜（净值/消费）
还是其他场景，都调本函数。阈值与文案改动只需改这一处。

约定：net_worth 含义由调用方决定（个人/财富榜 net_worth = cash - debt
+ 持仓 LMSR 清算价；消费榜 = 兑换消费总额 - 当前债务）。负值/0 都落到
最低档"初入幻想乡"。
"""
from decimal import Decimal


def rank_title(net_worth: Decimal) -> str:
    # 固定的一组净值档位（与可配置的初始余额 site_config.initial_balance 无强绑定）。
    # 最低档"人类灵(已爆仓)"含 ≤300 净值，避免新号一注册就解锁高称号。
    if net_worth > 30000: return "ZUN"
    if net_worth > 10000: return "炒炒币大亨"
    if net_worth > 3000:  return "妖怪操盘手"
    if net_worth > 1000:  return "天狗交易员"
    if net_worth > 300:   return "人里居民"
    return "人类灵(已爆仓)"
