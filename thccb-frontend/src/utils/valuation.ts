/**
 * 持仓估值纯函数层（阶段 3）。store 只做接线，可测逻辑全在这里。
 * 口径与后端 services/wealth.py 镜像（docs/holdings-value-semantics.md）：
 *   MTM 计入 HALT（账面口径，避免临时 HALT 让账面归零）
 *   LCV 只计 TRADING（立即变现口径，HALT 持仓 market_value=0、浮盈=-cost_basis）
 * 这些是显示口径；margin_status/强平/排行榜的权威判定仍在服务端（spec §6.3）。
 */
import { lcvValue, mtmValue } from './lmsr'
import type { Holding, HoldingSlim, MarketPriceCtx, RankThreshold, SummaryPosition } from '@/types/user'

function priceOf(
  ctx: Map<number, MarketPriceCtx>, marketId: number, outcomeId: number,
): { p: number; b: number; trading: boolean } | null {
  const m = ctx.get(marketId)
  if (!m) return null
  const idx = m.outcomeIds.indexOf(outcomeId)
  if (idx < 0) return null
  return { p: m.prices[idx]!, b: m.b, trading: m.status === 'trading' }
}

export function computeHoldingsValueMtm(
  positions: SummaryPosition[], ctx: Map<number, MarketPriceCtx>,
): number {
  let total = 0
  for (const pos of positions) {
    const c = priceOf(ctx, pos.market_id, pos.outcome_id)
    if (!c) continue
    total += mtmValue(pos.amount, c.p)
  }
  return total
}

export function computeHoldingsValueLcv(
  positions: SummaryPosition[], ctx: Map<number, MarketPriceCtx>, sellFeeRate: number,
): number {
  let total = 0
  for (const pos of positions) {
    const c = priceOf(ctx, pos.market_id, pos.outcome_id)
    if (!c || !c.trading) continue
    total += lcvValue(pos.amount, c.p, c.b, sellFeeRate)
  }
  return total
}

export function enrichHolding(
  h: HoldingSlim, ctx: Map<number, MarketPriceCtx>, sellFeeRate: number,
): Holding {
  const c = priceOf(ctx, h.market_id, h.outcome_id)
  const price = c?.p ?? 0
  const mtm = c ? mtmValue(h.amount, price) : 0
  const marketValue = c && c.trading ? lcvValue(h.amount, price, c.b, sellFeeRate) : 0
  return {
    ...h,
    avg_price: h.amount > 0 ? h.cost_basis / h.amount : 0,
    current_price: price,
    market_value: marketValue,
    unrealized_pnl: mtm - h.cost_basis,
    unrealized_pnl_liquidation: c && c.trading ? marketValue - h.cost_basis : -h.cost_basis,
  }
}

export function rankFromThresholds(table: RankThreshold[], netWorth: number): string {
  for (const t of table) {
    if (t.min_net_worth === null || netWorth > t.min_net_worth) return t.title
  }
  return table.length ? table[table.length - 1]!.title : ''
}

export interface FillArgs {
  side: 'buy' | 'sell'
  outcomeId: number
  shares: number
  /** buy=实付现金；sell=到手净额。调用方由 |旧cash − new_cash| 推导（6dp 精确） */
  pay: number
}

/**
 * 把一笔成交 apply 到仓位行数组（就地修改）。逻辑与后端 op_buy/op_sell 一致：
 * buy 累加；sell 先按卖出比例减 cost_basis 再减 amount，清仓移除整行。
 * 返回 false 表示 buy 未命中已有行（调用方负责 push 新行）。
 */
export function applyFillToRows(
  rows: { outcome_id: number; amount: number; cost_basis: number }[],
  args: FillArgs,
): boolean {
  const i = rows.findIndex(r => r.outcome_id === args.outcomeId)
  if (args.side === 'buy') {
    if (i < 0) return false
    rows[i]!.amount += args.shares
    rows[i]!.cost_basis += args.pay
    return true
  }
  if (i < 0) return true   // sell 无仓位：服务端已拒，本地无事可做
  const row = rows[i]!
  const ratio = args.shares / row.amount
  row.cost_basis -= row.cost_basis * ratio
  row.amount -= args.shares
  if (row.amount <= 1e-9) rows.splice(i, 1)
  return true
}
