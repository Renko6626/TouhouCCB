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
