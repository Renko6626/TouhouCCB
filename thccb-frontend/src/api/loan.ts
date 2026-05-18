import api from './index'

// ===== Types =====

export interface LoanQuota {
  enabled: boolean
  cash: string
  debt: string
  net_worth: string
  leverage_k: string
  daily_rate: string
  max_borrow: string
  last_accrued_at: string | null
}

export interface LoanActionResult {
  cash: string
  debt: string
  max_borrow: string
  /** 实际生效金额（仅 repay 有意义；用户输 3000 但只欠 1000，effective 会是 ~1000） */
  effective?: string | null
}

export interface SiteConfigItem {
  key: string
  value: string
  value_type: string
  updated_at: string
  updated_by: number | null
}

// ===== Player =====

export const loanApi = {
  async quota(): Promise<LoanQuota> {
    return api.get<LoanQuota>('/api/v1/loan/quota')
  },
  async borrow(amount: string): Promise<LoanActionResult> {
    return api.post<LoanActionResult>('/api/v1/loan/borrow', { amount })
  },
  async repay(amount: string): Promise<LoanActionResult> {
    return api.post<LoanActionResult>('/api/v1/loan/repay', { amount })
  },
}

export interface LiquidationEvent {
  id: number
  user_id: number
  username: string
  triggered_at: string
  pre_cash: string
  pre_debt: string
  pre_holdings_value: string
  pre_net_worth: string
  pre_margin_ratio: string
  sold_positions_count: number
  total_proceeds: string
  repaid_amount: string
  remaining_debt: string
  post_cash: string
  trigger_source: string
  fully_liquidated: boolean
}

export async function fetchRecentLiquidations(limit = 10): Promise<LiquidationEvent[]> {
  return api.get<LiquidationEvent[]>('/api/v1/loan/recent-liquidations', { params: { limit } })
}

// ===== Admin =====

export const adminSiteConfigApi = {
  async list(): Promise<SiteConfigItem[]> {
    return api.get<SiteConfigItem[]>('/api/v1/admin/site-config')
  },
  async update(key: string, value: string): Promise<SiteConfigItem> {
    return api.put<SiteConfigItem>(`/api/v1/admin/site-config/${key}`, { value })
  },
  async forceLoan(userId: number, amount: string, reason: string): Promise<any> {
    return api.post(`/api/v1/user/${userId}/force-loan`, { amount, reason })
  },
  async forgiveDebt(userId: number, amount: string, reason: string): Promise<any> {
    return api.post(`/api/v1/user/${userId}/forgive-debt`, { amount, reason })
  },
}

export default loanApi
