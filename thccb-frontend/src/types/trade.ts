// 交易相关类型定义
import type { Outcome } from './market'

export interface TradeRequest {
  outcome_id: number
  shares: number
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
