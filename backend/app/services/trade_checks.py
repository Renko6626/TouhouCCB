"""买卖滑点校验纯函数——老路径（market.py）与 writer 新路径共用单一实现。

从 market.py 提取，逻辑与文案逐字保持；改这里 = 同时改两条路径。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import HTTPException

# ── 滑点保护（P1）──
# 客户端未给 max_cost/min_proceeds 时用百分比兜底；服务端用 hardcap 截断不信任客户端。
DEFAULT_SLIPPAGE_BPS = 500   # 5%
HARDCAP_SLIPPAGE_BPS = 1000  # 10%，再大也截掉


def check_buy_slippage(
    pay: Decimal,
    expected_pay: Decimal,
    marginal_price: Decimal,
    max_cost: Optional[Decimal],
    max_slippage_bps: Optional[int],
    accept_any_slippage: bool,
) -> None:
    if max_cost is not None and pay > max_cost:
        raise HTTPException(
            status_code=400,
            detail=f"成交成本 {pay} 超过 max_cost 限制 {max_cost}，滑点过大请刷新报价",
        )
    if not accept_any_slippage:
        client_bps = max_slippage_bps if max_slippage_bps is not None else DEFAULT_SLIPPAGE_BPS
        effective_bps = min(client_bps, HARDCAP_SLIPPAGE_BPS)
        slippage_limit = (
            expected_pay * Decimal(10000 + effective_bps) / Decimal(10000)
        ).quantize(Decimal("0.000001"))
        if pay > slippage_limit:
            raise HTTPException(
                status_code=400,
                detail=f"滑点超过 {effective_bps / 100}%（边际价 {marginal_price}），请刷新报价",
            )


def check_sell_slippage(
    proceeds: Decimal,
    net: Decimal,
    expected_proceeds: Decimal,
    marginal_price: Decimal,
    min_proceeds: Optional[Decimal],
    max_slippage_bps: Optional[int],
    accept_any_slippage: bool,
) -> None:
    if min_proceeds is not None and net < min_proceeds:
        raise HTTPException(
            status_code=400,
            detail=f"成交收入 {net} 低于 min_proceeds 限制 {min_proceeds}，滑点过大请刷新报价",
        )
    if not accept_any_slippage:
        client_bps = max_slippage_bps if max_slippage_bps is not None else DEFAULT_SLIPPAGE_BPS
        effective_bps = min(client_bps, HARDCAP_SLIPPAGE_BPS)
        slippage_floor = (
            expected_proceeds * Decimal(10000 - effective_bps) / Decimal(10000)
        ).quantize(Decimal("0.000001"))
        if proceeds < slippage_floor:
            raise HTTPException(
                status_code=400,
                detail=f"滑点超过 {effective_bps / 100}%（边际价 {marginal_price}），请刷新报价",
            )
