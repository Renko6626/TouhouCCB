// 市场相关类型定义

export interface Market {
  id: number
  title: string
  description: string
  liquidity_b: number
  status: 'trading' | 'halt' | 'settled'
  created_at: string
  closes_at?: string | null
  tags?: string[]
  winning_outcome_id?: number
  settled_at?: string
  settled_by_user_id?: number
}

export interface Outcome {
  id: number
  label: string
  shares: number
  current_price: number
  price_change_24h?: number | null
  price_change_pct_24h?: number | null
}

export interface OrderBookEntry {
  price: number
  shares: number
  total_shares?: number
}

export interface OutcomeQuote {
  id: number
  label: string
  total_shares: number
  current_price: number
  payout?: number | null
  is_winner?: boolean | null
  price_change_24h?: number | null
  price_change_pct_24h?: number | null
}

export interface MarketListItem {
  id: number
  title: string
  description?: string
  liquidity_b: number
  status: string
  closes_at?: string | null
  tags?: string[]
  outcomes: Outcome[]
}

export interface MarketDetail {
  id: number
  title: string
  description: string
  status: string
  liquidity_b: number
  created_at: string
  closes_at?: string | null
  tags?: string[]
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
  liquidity_b: number
  outcomes: string[]
  closes_at?: string | null
  tags?: string[]
}

export interface MarketCreateResponse {
  status: string
  market_id: number
  title: string
  outcomes: string[]
  created_by: string
}

// 结算相关类型
export interface ResolveRequest {
  winning_outcome_id: number
  payout: number
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
