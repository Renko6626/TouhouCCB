"""品种键与命名空间（计划 §3.2 冻结签名，纯函数）。

统一品种键是 ``(product, id)``：LMSR 的 market_id 与 FX 的 pair_id 数值可能相同，
行情 / 缓存 / 限流 / 审计的命名空间必须带产品前缀，不能共用（spec §2.1）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Sequence

if TYPE_CHECKING:  # 避免与 valuation 循环 import（运行期只需鸭子类型）
    from app.services.credit.valuation import GroupLiquidation


Product = Literal["lmsr", "fx"]

# 与 GroupKey 的字典序一致（"fx" < "lmsr"），F12 冻结：组排序 (-L, product, group_id)。
PRODUCTS: tuple[Product, ...] = ("fx", "lmsr")

#: 未知组值（空头 K 不可报价）的排序幅度：``-magnitude`` 为 +Infinity，排到最后。
_UNKNOWN_MAGNITUDE = Decimal("-Infinity")


@dataclass(frozen=True, order=True)
class GroupKey:
    """一个可清算组：LMSR 的一个 market，或 FX 的一个 pair。

    ``order=True`` 给出 ``(product, group_id)`` 全序，即 F12 要求的 tie-break 顺序。
    """

    product: Product
    group_id: int

    def __post_init__(self) -> None:
        if self.product not in PRODUCTS:
            raise ValueError(f"unknown product: {self.product!r}")
        if self.group_id <= 0:
            raise ValueError(f"group_id must be positive: {self.group_id!r}")


def symbol_namespace(product: Product, group_id: int) -> str:
    """行情 / 缓存 / 限流键的产品命名空间：``"lmsr:{id}"`` / ``"fx:{id}"``。"""
    if product not in PRODUCTS:
        raise ValueError(f"unknown product: {product!r}")
    return f"{product}:{int(group_id)}"


def group_sort_key(group) -> tuple[Decimal, str, int]:
    """F12 冻结排序键：``(-L, product, group_id)``。

    WP2b1：空头回补组的 ``value`` 是**正的回补成本 K**（不是负回收），未知 K 为
    ``None``。未知组排在最后（排序幅度取 −Infinity，使 ``-magnitude`` 为
    +Infinity），不参与数值比较；已知的空头成本与正资产净回收共用同一"按绝对
    金额降序"顺序（spec §8.1 第 2 步）。
    """
    magnitude = _UNKNOWN_MAGNITUDE if group.value is None else group.value
    return (-magnitude, group.key.product, group.key.group_id)


def sort_groups_by_liquidation(groups: Sequence["GroupLiquidation"]) -> list["GroupLiquidation"]:
    """按**当前整组净清算价值降序**（同值按 ``(product, group_id)`` 升序）排序。

    只排序，不筛选：可执行性由调用方按 ``GroupLiquidation.executable`` 判断
    （spec §5.2 第 3 步"选择一个可卖组"，不可卖组跳过但要记录阻塞原因）。
    """
    return sorted(groups, key=group_sort_key)
