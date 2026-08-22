// 估值纯函数层：HALT 语义与后端 wealth.py 镜像；applyFill 与 op_buy/op_sell 一致
import { describe, expect, it } from 'vitest'
import {
  computeHoldingsValueMtm, computeHoldingsValueLcv,
  enrichHolding, rankFromThresholds, applyFillToRows,
} from '../valuation'
import { lcvValue } from '../lmsr'
import type { MarketPriceCtx, RankThreshold, SummaryPosition } from '@/types/user'

const ctx = new Map<number, MarketPriceCtx>([
  [1, { b: 100, status: 'trading', title: 'm1', outcomeIds: [11, 12], prices: [0.6, 0.4], outcomeLabels: ['a', 'b'], prices24hAgo: [null, null] }],
  [2, { b: 100, status: 'halt', title: 'm2', outcomeIds: [21, 22], prices: [0.7, 0.3], outcomeLabels: ['a', 'b'], prices24hAgo: [null, null] }],
])

const positions: SummaryPosition[] = [
  { outcome_id: 11, market_id: 1, amount: 10, cost_basis: 5 },
  { outcome_id: 21, market_id: 2, amount: 20, cost_basis: 12 },
  { outcome_id: 99, market_id: 9, amount: 7, cost_basis: 3 },  // 无价格上下文
]

describe('holdings 估值口径（与 wealth.py 镜像）', () => {
  it('MTM 计入 HALT，缺上下文跳过', () => {
    expect(computeHoldingsValueMtm(positions, ctx))
      .toBeCloseTo(10 * 0.6 + 20 * 0.7, 12)
  })
  it('LCV 只计 trading 市场', () => {
    const fee = 0.01
    expect(computeHoldingsValueLcv(positions, ctx, fee))
      .toBeCloseTo(lcvValue(10, 0.6, 100, fee), 12)
  })
})

describe('enrichHolding', () => {
  const slim = { market_id: 1, market_title: 't', outcome_id: 11,
                 outcome_label: 'a', amount: 10, cost_basis: 5 }
  it('trading 市场：全字段派生', () => {
    const h = enrichHolding(slim, ctx, 0.01)
    expect(h.avg_price).toBeCloseTo(0.5, 12)
    expect(h.current_price).toBe(0.6)
    expect(h.market_value).toBeCloseTo(lcvValue(10, 0.6, 100, 0.01), 12)
    expect(h.unrealized_pnl).toBeCloseTo(10 * 0.6 - 5, 12)
    expect(h.unrealized_pnl_liquidation).toBeCloseTo(h.market_value - 5, 12)
  })
  it('HALT 市场：market_value=0、LCV 浮盈=-cost_basis、MTM 浮盈正常', () => {
    const h = enrichHolding({ ...slim, market_id: 2, outcome_id: 21,
                              amount: 20, cost_basis: 12 }, ctx, 0.01)
    expect(h.market_value).toBe(0)
    expect(h.unrealized_pnl_liquidation).toBe(-12)
    expect(h.unrealized_pnl).toBeCloseTo(20 * 0.7 - 12, 12)
  })
})

describe('rankFromThresholds（与后端 rank_title 同规则）', () => {
  const table: RankThreshold[] = [
    { min_net_worth: 30000, title: 'ZUN' },
    { min_net_worth: 300, title: '人里居民' },
    { min_net_worth: null, title: '人类灵(已爆仓)' },
  ]
  it('严格大于；等于阈值落下一档；空表返回空串', () => {
    expect(rankFromThresholds(table, 30000.01)).toBe('ZUN')
    expect(rankFromThresholds(table, 30000)).toBe('人里居民')
    expect(rankFromThresholds(table, 300)).toBe('人类灵(已爆仓)')
    expect(rankFromThresholds(table, -1)).toBe('人类灵(已爆仓)')
    expect(rankFromThresholds([], 100)).toBe('')
  })
})

describe('applyFillToRows（与后端 op_buy/op_sell 一致）', () => {
  it('buy：已有仓位累加', () => {
    const rows = [{ outcome_id: 11, amount: 10, cost_basis: 5 }]
    applyFillToRows(rows, { side: 'buy', outcomeId: 11, shares: 3, pay: 2 })
    expect(rows[0]).toMatchObject({ amount: 13, cost_basis: 7 })
  })
  it('buy：新仓位由调用方补行（函数返回 false 表示未命中）', () => {
    const rows: { outcome_id: number; amount: number; cost_basis: number }[] = []
    expect(applyFillToRows(rows, { side: 'buy', outcomeId: 11, shares: 3, pay: 2 }))
      .toBe(false)
  })
  it('sell：先按卖出比例减成本再减数量；清仓移除', () => {
    const rows = [{ outcome_id: 11, amount: 10, cost_basis: 5 }]
    applyFillToRows(rows, { side: 'sell', outcomeId: 11, shares: 4, pay: 3 })
    expect(rows[0]!.amount).toBeCloseTo(6, 12)
    expect(rows[0]!.cost_basis).toBeCloseTo(3, 12)   // 5 × (1 - 4/10)
    applyFillToRows(rows, { side: 'sell', outcomeId: 11, shares: 6, pay: 3 })
    expect(rows.length).toBe(0)
  })
})
