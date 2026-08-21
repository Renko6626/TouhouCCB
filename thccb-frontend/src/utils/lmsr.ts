/**
 * LMSR 闭式公式 —— 客户端计算契约的单一实现（spec §6.1 / §6.2）。
 *
 * 只需要当前价 p 与流动性 b，不需要 q：
 *   ΔC   = b · log1p( p_i · expm1(Δ/b) )                买入成本
 *   D    = 1 + p_i · expm1(Δ/b)
 *   p'_i = p_i · exp(Δ/b) / D，p'_j = p_j / D (j ≠ i)   成交后价格
 *   卖出把 Δ 换成 −Δ，ΔC 为负，收入 = −ΔC
 *
 * 数值要点（spec §6.2，缺一不可）：
 *   1. 用 log1p/expm1 —— 小额交易 Δ/b 极小时朴素 exp(x)−1 丢 2-3 位有效数字
 *   2. Δ/b > 700 走渐近分支 ΔC → Δ + b·ln(p_i)，否则 expm1 溢出成 Infinity
 *   3. 与服务端偏差分两层（数学层 ~1e-15、线上 8dp 输入层 ~1e-7 相对），
 *      都是已知且接受的——这里算的是预览，成交以 writer 返回为准（§6.3）
 */

const ASYMPTOTIC_X = 700

/** 买入 delta 份的 LMSR 成本（未扣费；买入无费）。非法输入返回 0。 */
export function buyCost(p: number, delta: number, b: number): number {
  if (delta <= 0 || p <= 0 || b <= 0) return 0
  const x = delta / b
  if (x > ASYMPTOTIC_X) return delta + b * Math.log(p)
  return b * Math.log1p(p * Math.expm1(x))
}

/** 卖出 delta 份的 LMSR 收入（正数，未扣 sell_fee）。非法输入返回 0。 */
export function sellProceeds(p: number, delta: number, b: number): number {
  if (delta <= 0 || p <= 0 || b <= 0) return 0
  // p·expm1(−x) ∈ (−p, 0]，数学上恒 > −1；p≈1 且 delta 大时浮点可能贴到 −1，
  // clamp 防 log1p(−1) = −Infinity
  const arg = Math.max(p * Math.expm1(-delta / b), -1 + 1e-15)
  return -b * Math.log1p(arg)
}

/** 成交后的全市场价格向量。delta>0 买入 / delta<0 卖出（idx 为被交易项）。 */
export function pricesAfterTrade(
  prices: number[], idx: number, delta: number, b: number,
): number[] {
  const x = delta / b
  if (x > ASYMPTOTIC_X) {
    // 渐近：被买爆的项价格 → 1，其余 → 0
    return prices.map((_, i) => (i === idx ? 1 : 0))
  }
  const D = 1 + prices[idx]! * Math.expm1(x)
  return prices.map((p, i) => (i === idx ? (p * Math.exp(x)) / D : p / D))
}

/** MTM 账面估值 = 数量 × 瞬时价（不含滑点不扣费，spec §6.1）。 */
export function mtmValue(amount: number, p: number): number {
  return amount * p
}

/** LCV 立即清算价值 = 全卖 LMSR 收入 × (1 − sell_fee_rate)（含滑点，spec §6.1）。 */
export function lcvValue(
  amount: number, p: number, b: number, sellFeeRate: number,
): number {
  return sellProceeds(p, amount, b) * (1 - sellFeeRate)
}
