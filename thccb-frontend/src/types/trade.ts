// 交易相关类型定义
import type { Outcome } from './market'

export interface TradeRequest {
  outcome_id: number
  shares: number
  // ── 滑点保护（P1，后端 hardcap=1000bps=10%）──
  max_cost?: number          // 买入最高可接受成本（含费）；优先级高于 bps
  min_proceeds?: number      // 卖出最低可接受收入（净）；优先级高于 bps
  max_slippage_bps?: number  // 最大滑点，万分之一（100=1%），默认 500=5%
  accept_any_slippage?: boolean  // true 时跳过 max_slippage_bps 检查（平仓/大额建仓用）
}

export interface TradeResponse {
  shares: number
  cost: number          // 2dp 展示用
  new_cash: number      // 6dp
  pay: number           // 6dp 精确成交额：买入=实付、卖出=净收。本地 apply 的 cost_basis 基线
  message: string
}

/** 以本地预览价为基准的绝对价格保护（见 TradingView.executeTrade） */
export interface TradeLimit {
  max_cost?: number
  min_proceeds?: number
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
  // 阶段 3 起 QuoteResponse 由前端本地构造（utils/lmsr 预览），不再来自
  // /market/quote；after_prices 本地不算，置空。后端端点保留给 bot。
  after_prices?: Outcome[]

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
