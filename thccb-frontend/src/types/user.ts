// 用户相关类型定义

export interface User {
  id: number
  email: string
  username: string
  cash: number
  debt: number
  is_active: boolean
  is_superuser: boolean
  is_verified: boolean
}

export interface UserSummary {
  cash: number
  debt: number
  holdings_value: number
  net_worth: number
  rank: string
}

export interface Holding {
  market_id: number
  market_title: string
  outcome_id: number
  outcome_label: string
  amount: number
}

export interface Transaction {
  id: number
  type: 'buy' | 'sell' | 'settle'
  shares: number
  price: number
  cost: number
  timestamp: string
}

// 用户创建类型
export interface UserCreate {
  email: string
  password: string
  username: string
  cash?: number  // 默认100.0
  is_superuser?: boolean  // 默认false
}

// 用户更新类型
export interface UserUpdate {
  username?: string
  cash?: number
  is_superuser?: boolean
}

// 排行榜相关类型
export interface LeaderboardItem {
  user_id: number
  username: string
  net_worth: number
  rank: string
}