// utils/lmsr.ts golden 对拍（spec §6.6 两层，与 §6.2 第 3 条的两层偏差一一对应）：
//   数学层：喂全精度 p，与后端 services/lmsr.py 对拍，相对误差 < 1e-12
//   线上层：喂 8dp 量化 p（真实线上输入），相对误差 < 1e-6
// fixture 由 backend/scripts/gen_lmsr_golden.py 生成（服务端 q 路径权威值）。
import { describe, expect, it } from 'vitest'
import { buyCost, sellProceeds, pricesAfterTrade, mtmValue, lcvValue } from '../lmsr'
import golden from './lmsr.golden.json'

const relErr = (actual: number, expected: number) =>
  Math.abs(actual - expected) / Math.max(Math.abs(expected), 1e-300)

interface TradeCase {
  b: number; idx: number; delta: number; side: string
  p_full: number[]; p_8dp: number[]
  expected_amount: number; expected_prices_after: number[]
}
interface HoldingCase {
  b: number; idx: number; amount: number; sell_fee_rate: number
  p_full: number[]; p_8dp: number[]
  expected_mtm: number; expected_lcv: number
}

const tradeAmount = (c: TradeCase, prices: number[]) =>
  c.side === 'buy'
    ? buyCost(prices[c.idx]!, c.delta, c.b)
    : sellProceeds(prices[c.idx]!, c.delta, c.b)

describe('utils/lmsr golden 对拍', () => {
  for (const [i, c] of (golden.trades as TradeCase[]).entries()) {
    it(`数学层 trade#${i} ${c.side} Δ=${c.delta} b=${c.b}`, () => {
      expect(relErr(tradeAmount(c, c.p_full), c.expected_amount)).toBeLessThan(1e-12)
      const after = pricesAfterTrade(
        c.p_full, c.idx, c.side === 'buy' ? c.delta : -c.delta, c.b)
      for (let j = 0; j < after.length; j++) {
        // 价格用绝对误差（价格 ∈ [0,1]，渐近 case 的 0 价无相对误差可言）
        expect(Math.abs(after[j]! - c.expected_prices_after[j]!)).toBeLessThan(1e-12)
      }
    })
    it(`线上层 trade#${i}（8dp 量化输入）`, () => {
      expect(relErr(tradeAmount(c, c.p_8dp), c.expected_amount)).toBeLessThan(1e-6)
    })
  }

  for (const [i, c] of (golden.holdings as HoldingCase[]).entries()) {
    it(`数学层 holding#${i}`, () => {
      expect(relErr(mtmValue(c.amount, c.p_full[c.idx]!), c.expected_mtm)).toBeLessThan(1e-12)
      expect(relErr(lcvValue(c.amount, c.p_full[c.idx]!, c.b, c.sell_fee_rate),
                    c.expected_lcv)).toBeLessThan(1e-12)
    })
    it(`线上层 holding#${i}（8dp 量化输入）`, () => {
      expect(relErr(lcvValue(c.amount, c.p_8dp[c.idx]!, c.b, c.sell_fee_rate),
                    c.expected_lcv)).toBeLessThan(1e-6)
    })
  }
})

describe('utils/lmsr 边界', () => {
  it('小额交易走 log1p/expm1 不丢有效位：Δ/b=0.01 与 golden 首例覆盖', () => {
    // buyCost 单调性 sanity：同 p 下 Δ 翻倍成本大于 2 倍单价×Δ 的线性差
    const c1 = buyCost(0.5, 1, 100)
    const c2 = buyCost(0.5, 2, 100)
    expect(c2).toBeGreaterThan(2 * c1 * 0.999)
  })
  it('Δ/b > 700 渐近分支不溢出', () => {
    const c = buyCost(0.5, 80000, 100)
    expect(Number.isFinite(c)).toBe(true)
    expect(relErr(c, 80000 + 100 * Math.log(0.5))).toBeLessThan(1e-12)
  })
  it('p≈1 大额卖出不产生 -Infinity（log1p(-1) clamp）', () => {
    expect(Number.isFinite(sellProceeds(0.99999999, 100000, 100))).toBe(true)
  })
  it('非法输入返回 0', () => {
    expect(buyCost(0, 10, 100)).toBe(0)
    expect(sellProceeds(0.5, 0, 100)).toBe(0)
  })
})
