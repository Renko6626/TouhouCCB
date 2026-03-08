import math
from typing import List

def calculate_lmsr_cost(shares_list: List[float], b: float) -> float:
    """
    计算当前全场份额分布下的系统总成本 C = b * ln(sum(e^(qi/b)))
    """
    if not shares_list: return 0.0
    max_q = max(shares_list)
    # 防止指数爆炸：e^(q/b) = e^((q-max_q)/b) * e^(max_q/b)
    sum_exp = sum(math.exp((q - max_q) / b) for q in shares_list)
    return b * (math.log(sum_exp) + (max_q / b))

def get_current_price(shares_list: List[float], target_index: int, b: float) -> float:
    """计算某个选项的瞬时单价 P = e^(qi/b) / sum(e^(qj/b))"""
    max_q = max(shares_list)
    exponents = [math.exp((q - max_q) / b) for q in shares_list]
    return exponents[target_index] / sum(exponents)