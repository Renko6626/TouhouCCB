// 用户相关类型定义

export interface User {
  id: number
  email: string
  username: string
  cash: number
  debt: number
  is_active: boolean
  is_superuser: boolean
  tos_accepted_at: string | null
}

export interface UserSummary {
  cash: number
  debt: number
  /** MTM 主显示：瞬时价 × 数量，不含滑点 */
  holdings_value: number
  /** LCV 保守口径：LMSR 清算价值（含滑点 + 扣 sell_fee），用于借款额度/margin 判定 */
  holdings_value_liquidation: number
  total_cost_basis: number
  /** = holdings_value (MTM) - total_cost_basis */
  unrealized_pnl: number
  /** MTM 主显示：cash - debt + holdings_value */
  net_worth: number
  /** LCV 保守：cash - debt + holdings_value_liquidation，margin 按此算 */
  net_worth_liquidation: number
  rank: string
  /** = net_worth_liquidation / debt，比 MTM 口径保守 */
  margin_ratio: number | null
  margin_status: 'healthy' | 'warning' | 'danger'
  last_liquidated_at: string | null
  margin_hard_threshold: number
  margin_soft_threshold: number
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
  market_id?: number | null
  market_title?: string | null
  outcome_label?: string | null
  type: 'buy' | 'sell' | 'settle' | 'settle_lose' | 'liquidate'
  shares: number
  price: number
  gross: number
  fee: number
  cost: number
  timestamp: string
}

// 排行榜相关类型
export type LeaderboardMode = 'net_worth' | 'spending'

export interface LeaderboardItem {
  user_id: number
  username: string
  // 排序分值：net_worth 模式 = cash - debt；spending 模式 = 兑换消费总额 - 当前债务
  net_worth: number
  rank: string
  // spending 模式额外字段
  spent_total?: number | null
  debt?: number | null
}
