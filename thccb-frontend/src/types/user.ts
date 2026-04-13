// 用户相关类型定义

export interface User {
  id: number
  email: string
  username: string
  cash: number
  debt: number
  is_active: boolean
  is_superuser: boolean
}

export interface UserSummary {
  cash: number
  debt: number
  holdings_value: number
  total_cost_basis: number
  unrealized_pnl: number
  net_worth: number
  rank: string
}

export interface Holding {
  market_id: number
  market_title: string
  outcome_id: number
  outcome_label: string
  amount: number
  cost_basis: number
  avg_price: number
  current_price: number
  market_value: number
  unrealized_pnl: number
}

export interface Transaction {
  id: number
  outcome_id: number
  type: 'buy' | 'sell' | 'settle' | 'settle_lose'
  shares: number
  price: number
  gross: number
  fee: number
  cost: number
  timestamp: string
}

// 排行榜相关类型
export interface LeaderboardItem {
  user_id: number
  username: string
  net_worth: number
  rank: string
}
