"""统一的用户称号体系。

全站只按 net_worth 一个口径定称号——无论是个人 summary、排行榜（净值/消费）
还是其他场景，都调本函数。阈值与文案改动只需改这一处。

约定：net_worth 含义由调用方决定（个人/榜单 net_worth = cash - debt；
消费榜 = 兑换消费总额 - 当前债务）。负值/0 都落到最低档"初入幻想乡"。
"""
from decimal import Decimal


def rank_title(net_worth: Decimal) -> str:
    # 阈值锚定新用户起始资金（INITIAL_BALANCE=500）：起始档"初入幻想乡"含起始
    # 资金；需要至少把净值翻倍才能升到下一档，避免新号一注册就解锁称号。
    if net_worth > 50000: return "大天狗座上宾"
    if net_worth > 10000: return "守矢VIP"
    if net_worth > 2000:  return "命莲寺赞助者"
    if net_worth > 1000:  return "人间之里商人"
    return "初入幻想乡"
