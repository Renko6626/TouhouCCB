"""本地复现后端 LMSR 报价，用于策略本地预判。

数学严格对齐 backend/app/services/lmsr.py，差异 < 1e-8。
"""
import math
from typing import List, Tuple


def calculate_lmsr_cost(shares_list: List[float], b: float) -> float:
    if not shares_list:
        return 0.0
    max_q = max(shares_list)
    sum_exp = sum(math.exp((q - max_q) / b) for q in shares_list)
    return b * (math.log(sum_exp) + (max_q / b))


def get_current_price(shares_list: List[float], target_index: int, b: float) -> float:
    max_q = max(shares_list)
    exponents = [math.exp((q - max_q) / b) for q in shares_list]
    return exponents[target_index] / sum(exponents)


def calculate_lmsr_with_prices(
    shares_list: List[float], b: float
) -> Tuple[float, List[float]]:
    if not shares_list:
        return 0.0, []
    max_q = max(shares_list)
    exponents = [math.exp((q - max_q) / b) for q in shares_list]
    sum_exp = sum(exponents)
    cost = b * (math.log(sum_exp) + (max_q / b))
    prices = [e / sum_exp for e in exponents]
    return cost, prices


def estimate_buy_cost(
    shares_list: List[float], target_index: int, delta_shares: float, b: float
) -> float:
    """模拟买入 delta_shares 后的成本差（即用户实付，未含手续费）。"""
    before = calculate_lmsr_cost(shares_list, b)
    after = list(shares_list)
    after[target_index] += delta_shares
    return calculate_lmsr_cost(after, b) - before


def estimate_sell_proceeds(
    shares_list: List[float], target_index: int, delta_shares: float, b: float
) -> float:
    """模拟卖出 delta_shares 后用户获得的金额（未扣手续费）。"""
    before = calculate_lmsr_cost(shares_list, b)
    after = list(shares_list)
    after[target_index] -= delta_shares
    return before - calculate_lmsr_cost(after, b)
