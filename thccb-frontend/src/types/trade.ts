// 交易相关类型定义
import type { Outcome } from './market'

export interface TradeRequest {
  outcome_id: number
  shares: number
  // ── 滑点保护（P1，后端 hardcap=1000bps=10%）──
  max_cost?: number          // 买入最高可接受成本（含费）；优先级高于 bps
  min_proceeds?: number      // 卖出最低可接受收入（净）；优先级高于 bps
  max_slippage_bps?: number  // 最大滑点，万分之一（100=1%），默认 500=5%
}

export interface TradeResponse {
  shares: number
  cost: number
  new_cash: number
  message: string
}

export interface QuoteRequest {
  outcome_id: number
  shares: number
  side: 'buy' | 'sell'
}

export interface QuoteResponse {
  outcome_id: number
  side: string
  shares: number
  avg_price: number        // 平均价格
  gross: number            // 总金额（不含手续费）
  fee: number              // 手续费
  net: number              // 净金额
  after_prices: Outcome[]
  
  // 计算属性（用于显示，与TradeResponse保持一致）
  price_per_share?: number // 前端计算，等于 avg_price
  cost?: number            // 前端计算，等于 net
}

export interface MarketTrade {
  id: number
  outcome_id: number
  side: 'buy' | 'sell'
  shares: number
  price: number
  gross: number
  fee: number
  timestamp: string
  username: string
}
