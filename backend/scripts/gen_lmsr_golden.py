"""生成 thccb-frontend/src/utils/__tests__/lmsr.golden.json（spec §6.6）。

用法（backend/ 目录下）：
    python scripts/gen_lmsr_golden.py > ../thccb-frontend/src/utils/__tests__/lmsr.golden.json

expected_* 一律由服务端 q 路径（app/services/lmsr.py，float 内核）算出——
这是「客户端从 p 算 vs 服务端从 q 算」对拍的权威侧。产出两层输入：
  p_full —— 全精度价格（数学层，前端断言相对误差 < 1e-12）
  p_8dp  —— quantize_price 后的 8 位小数价格（线上层，< 1e-6）
注意 expected 是纯数学口径（无 6dp 资金量化）；服务端资金量化粒度属
spec §6.2 层 (b) 之下的已知差异，不进本 fixture。

阈值与用例的边界约束：
  * delta ≥ 1 —— 更小的 delta 会让服务端 cost 差分先发生灾难性抵消，
    数学层 1e-12 就不再是客户端的误差而是服务端的
  * 用例价格 ≥ 0.05 —— 线上层 1e-6 相对误差在超低价上会被 8dp 量化主导
"""
import json
import sys
from decimal import Decimal

from app.services.lmsr import calculate_lmsr_with_prices, quantize_price

# (b, q 向量, 被交易 idx, delta, side)
TRADE_CASES = [
    (100.0, [0.0, 0.0], 0, 1.0, "buy"),                 # Δ/b=0.01：log1p/expm1 必要性
    (100.0, [3.5, 0.0], 0, 10.0, "buy"),
    (100.0, [3.5, 0.0], 1, 10.0, "buy"),
    (100.0, [120.0, 40.0, 77.5], 2, 50.0, "buy"),
    (500.0, [1000.0, 800.0], 1, 250.0, "buy"),
    (50.0, [10.0, 5.0, 0.0, 20.0], 0, 5.0, "buy"),
    (100.0, [0.0, 0.0], 0, 80000.0, "buy"),             # Δ/b=800：前端渐近分支
    (100.0, [50.0, 30.0], 0, 25.0, "sell"),
    (100.0, [120.0, 40.0, 77.5], 0, 120.0, "sell"),     # 全卖到 0
    (500.0, [1000.0, 800.0], 0, 400.0, "sell"),
]

# (b, q, idx, amount, sell_fee_rate) —— MTM/LCV 持仓估值
HOLDING_CASES = [
    (100.0, [50.0, 30.0], 0, 50.0, "0.01"),
    (100.0, [120.0, 40.0, 77.5], 1, 40.0, "0"),
    (500.0, [1000.0, 800.0], 1, 800.0, "0.005"),
]


def trade_case(b, q, idx, delta, side):
    cost0, prices0 = calculate_lmsr_with_prices(list(q), b)
    q2 = list(q)
    q2[idx] += delta if side == "buy" else -delta
    cost1, prices1 = calculate_lmsr_with_prices(q2, b)
    return {
        "b": b, "idx": idx, "delta": delta, "side": side,
        "p_full": prices0,
        "p_8dp": [float(quantize_price(p)) for p in prices0],
        "expected_amount": (cost1 - cost0) if side == "buy" else (cost0 - cost1),
        "expected_prices_after": prices1,
    }


def holding_case(b, q, idx, amount, fee):
    cost0, prices0 = calculate_lmsr_with_prices(list(q), b)
    q2 = list(q)
    q2[idx] -= amount
    cost1, _ = calculate_lmsr_with_prices(q2, b)
    fee_f = float(Decimal(fee))
    return {
        "b": b, "idx": idx, "amount": amount, "sell_fee_rate": fee_f,
        "p_full": prices0,
        "p_8dp": [float(quantize_price(p)) for p in prices0],
        "expected_mtm": amount * prices0[idx],
        "expected_lcv": (cost0 - cost1) * (1 - fee_f),
    }


def main():
    out = {
        "trades": [trade_case(*c) for c in TRADE_CASES],
        "holdings": [holding_case(*c) for c in HOLDING_CASES],
    }
    json.dump(out, sys.stdout, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
