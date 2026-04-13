import math
from decimal import Decimal, ROUND_HALF_UP
from typing import List

# ── 量化精度 ──
COST_QUANT  = Decimal("0.000001")    # 6 位小数：资金 / 份额
PRICE_QUANT = Decimal("0.00000001")  # 8 位小数：价格


def quantize_cost(value: float) -> Decimal:
    """LMSR float 结果 → Decimal(16,6)，用于资金和份额。"""
    return Decimal(str(value)).quantize(COST_QUANT, rounding=ROUND_HALF_UP)


def quantize_price(value: float) -> Decimal:
    """LMSR float 结果 → Decimal(16,8)，用于价格。"""
    return Decimal(str(value)).quantize(PRICE_QUANT, rounding=ROUND_HALF_UP)


# ── LMSR 核心（内部保持 float，数学运算不受影响）──

def calculate_lmsr_cost(shares_list: List[float], b: float) -> float:
    """
    计算当前全场份额分布下的系统总成本 C = b * ln(sum(e^(qi/b)))
    """
    if not shares_list:
        return 0.0
    max_q = max(shares_list)
    sum_exp = sum(math.exp((q - max_q) / b) for q in shares_list)
    return b * (math.log(sum_exp) + (max_q / b))


def get_current_price(shares_list: List[float], target_index: int, b: float) -> float:
    """计算某个选项的瞬时单价 P = e^(qi/b) / sum(e^(qj/b))"""
    max_q = max(shares_list)
    exponents = [math.exp((q - max_q) / b) for q in shares_list]
    return exponents[target_index] / sum(exponents)
