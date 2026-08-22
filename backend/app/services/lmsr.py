import math
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Tuple, Union

# ── 量化精度 ──
COST_QUANT  = Decimal("0.000001")    # 6 位小数：资金 / 份额
PRICE_QUANT = Decimal("0.00000001")  # 8 位小数：价格


def quantize_cost(value: Union[float, Decimal]) -> Decimal:
    """将 float 或 Decimal 量化到 Decimal(16,6)，用于资金和份额。"""
    if isinstance(value, Decimal):
        return value.quantize(COST_QUANT, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(COST_QUANT, rounding=ROUND_HALF_UP)


def quantize_price(value: Union[float, Decimal]) -> Decimal:
    """将 float 或 Decimal 量化到 Decimal(16,8)，用于价格。"""
    if isinstance(value, Decimal):
        return value.quantize(PRICE_QUANT, rounding=ROUND_HALF_UP)
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


def calculate_lmsr_with_prices(
    shares_list: List[float], b: float
) -> Tuple[float, List[float]]:
    """同时返回 (cost, [price_0, price_1, ..., price_{N-1}])。

    数学上 cost = b·ln(Σexp(qi/b))，price_i = exp(qi/b) / Σexp(qj/b)。
    两者共享同一组 exp 计算，合并一次完成可省 N 次 exp。
    """
    if not shares_list:
        return 0.0, []
    max_q = max(shares_list)
    exponents = [math.exp((q - max_q) / b) for q in shares_list]
    sum_exp = sum(exponents)
    cost = b * (math.log(sum_exp) + (max_q / b))
    prices = [e / sum_exp for e in exponents]
    return cost, prices


def seed_shares_from_prices(prices: List[float], b: float) -> List[Decimal]:
    """由先验价格反推初始份额：q_i = b·(ln p_i − ln p_min)。

    LMSR 价格是 softmax，q_i = b·ln p_i 时 price_i 恰好等于 p_i；整体平移 −b·ln p_min
    不改变价格（C(q+c)=C(q)+c），但保证 q ≥ 0 且 min 为 0，避免 chart 反推里的
    max(0, ·) 钳位与可读性问题。prices 需已归一化（和为 1，每项 > 0）。
    """
    if not prices:
        return []
    log_min = math.log(min(prices))
    return [quantize_cost(b * (math.log(p) - log_min)) for p in prices]
