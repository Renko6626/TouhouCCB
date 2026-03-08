// 市场相关类型定义

export interface Market {
  id: number
  title: string
  description: string
  liquidity_b: number
  status: 'trading' | 'halt' | 'settled'
  created_at: string
  winning_outcome_id?: number
  settled_at?: string
  settled_by_user_id?: number
}

export interface Outcome {
  id: number
  label: string
  shares: number
  current_price: number
}

export interface OutcomeQuote {
  id: number
  label: string
  total_shares: number
  current_price: number
  payout?: number
  is_winner?: boolean
}

export interface MarketListItem {
  id: number
  title: string
  description?: string
  liquidity_b: number
  status: string
  outcomes: Outcome[]
}

export interface MarketDetail {
  id: number
  title: string
  description: string
  status: string
  liquidity_b: number
  created_at: string
  winning_outcome_id?: number
  settled_at?: string
  settled_by_user_id?: number
  outcomes: OutcomeQuote[]
  last_trade_at?: string
}

// 市场创建请求类型
export interface MarketCreate {
  title: string
  description?: string
  liquidity_b: number  // > 0
  outcomes: string[]   // 至少2个选项
}

export interface MarketCreateResponse {
  status: string
  market_id: number
  title: string
  outcomes: string[]
  created_by: string
}

// 结算相关类型
export interface SettleRequest {
  winning_outcome_id: number
}

export interface ResolveRequest {
  winning_outcome_id: number
  payout: number  // >= 0
}

export interface MarketStatusResponse {
  message: string
}

export interface SettleResult {
  market_id: number
  status: string
  winning_outcome_id: number
  settled_at: string
  total_payout: number
  settled_positions: number
}