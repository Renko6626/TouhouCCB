"""WP1 纯函数：credit/keys.py 的品种键、命名空间与强平组排序（F12 冻结）。"""
import sys
import os
from dataclasses import dataclass
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from app.services.credit.keys import (
    PRODUCTS,
    GroupKey,
    group_sort_key,
    sort_groups_by_liquidation,
    symbol_namespace,
)


@dataclass(frozen=True)
class _FakeGroup:
    """valuation.GroupLiquidation 的结构替身（该模块属 WP2，本测试只验证排序契约）。"""

    key: GroupKey
    value: Decimal
    executable: bool = True
    blocked_reason: str | None = None


def test_group_key_orders_fx_before_lmsr():
    """F12：同值时 "fx" < "lmsr"（字符串序即产品序）。"""
    assert GroupKey("fx", 99) < GroupKey("lmsr", 1)
    assert sorted([GroupKey("lmsr", 1), GroupKey("fx", 2)]) == [
        GroupKey("fx", 2),
        GroupKey("lmsr", 1),
    ]


def test_group_key_same_product_orders_by_id():
    assert GroupKey("lmsr", 2) < GroupKey("lmsr", 10)
    assert GroupKey("fx", 1) == GroupKey("fx", 1)


def test_group_key_rejects_illegal_values():
    with pytest.raises(ValueError):
        GroupKey("stock", 1)          # type: ignore[arg-type]
    with pytest.raises(ValueError):
        GroupKey("lmsr", 0)
    with pytest.raises(ValueError):
        GroupKey("fx", -3)


def test_products_namespace_is_prefixed():
    assert PRODUCTS == ("fx", "lmsr")
    assert symbol_namespace("lmsr", 7) == "lmsr:7"
    assert symbol_namespace("fx", 7) == "fx:7"
    # 同号不同产品不得共用命名空间
    assert symbol_namespace("lmsr", 7) != symbol_namespace("fx", 7)
    with pytest.raises(ValueError):
        symbol_namespace("market", 7)   # type: ignore[arg-type]


def test_sort_groups_by_liquidation_is_value_desc_then_key_asc():
    groups = [
        _FakeGroup(GroupKey("lmsr", 5), Decimal("10")),
        _FakeGroup(GroupKey("fx", 3), Decimal("10")),
        _FakeGroup(GroupKey("lmsr", 1), Decimal("99.5")),
        _FakeGroup(GroupKey("fx", 1), Decimal("0")),
        _FakeGroup(GroupKey("fx", 2), Decimal("10")),
    ]
    ordered = sort_groups_by_liquidation(groups)
    assert [(g.key.product, g.key.group_id) for g in ordered] == [
        ("lmsr", 1),   # 99.5
        ("fx", 2),     # 10  ← 同值按 (product, id)
        ("fx", 3),
        ("lmsr", 5),
        ("fx", 1),     # 0
    ]
    assert group_sort_key(ordered[0]) == (Decimal("-99.5"), "lmsr", 1)


def test_sort_groups_handles_negative_and_empty_values():
    groups = [
        _FakeGroup(GroupKey("lmsr", 2), Decimal("-1")),
        _FakeGroup(GroupKey("fx", 1), Decimal("0.000001")),
    ]
    assert [g.key.group_id for g in sort_groups_by_liquidation(groups)] == [1, 2]
    assert sort_groups_by_liquidation([]) == []


def test_sort_keeps_blocked_groups_in_place():
    """排序只按 (-L, product, id)：可执行性由调用方过滤，不改变排序契约。"""
    blocked = _FakeGroup(GroupKey("fx", 1), Decimal("50"), executable=False,
                         blocked_reason="paused")
    ok = _FakeGroup(GroupKey("lmsr", 1), Decimal("1"))
    ordered = sort_groups_by_liquidation([ok, blocked])
    assert ordered[0] is blocked and ordered[1] is ok
