"""RANK_THRESHOLDS 查表与 rank_title 行为等价性。"""
from decimal import Decimal

from app.services.rank import RANK_THRESHOLDS, rank_title

D = Decimal


def test_thresholds_table_shape():
    """降序 5 档 + 1 个 None 兜底档；顺序即优先级。"""
    assert len(RANK_THRESHOLDS) == 6
    numeric = [t for t, _ in RANK_THRESHOLDS if t is not None]
    assert numeric == sorted(numeric, reverse=True)
    assert RANK_THRESHOLDS[-1] == (None, "人类灵(已爆仓)")


def test_rank_title_matches_legacy_behavior():
    """与旧 if 链逐界点等价：> 是严格大于，等于阈值落到下一档。"""
    cases = [
        (D("30000.01"), "ZUN"),
        (D("30000"), "炒炒币大亨"),      # 等于阈值不进上档
        (D("10000.01"), "炒炒币大亨"),
        (D("10000"), "妖怪操盘手"),
        (D("3000.01"), "妖怪操盘手"),
        (D("3000"), "天狗交易员"),
        (D("1000.01"), "天狗交易员"),
        (D("1000"), "人里居民"),
        (D("300.01"), "人里居民"),
        (D("300"), "人类灵(已爆仓)"),
        (D("0"), "人类灵(已爆仓)"),
        (D("-500"), "人类灵(已爆仓)"),
    ]
    for nw, expected in cases:
        assert rank_title(nw) == expected, f"net_worth={nw}"
