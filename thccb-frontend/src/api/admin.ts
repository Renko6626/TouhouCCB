import api from './index'

export interface UserListItem {
  id: number
  username: string
  cash: number
  debt: number
  is_active: boolean
  is_superuser: boolean
}

export interface AdjustCashResult {
  user_id: number
  username: string
  amount: number
  new_cash: number
  reason: string
}

export interface BatchAdjustFilter {
  user_id_min?: number | null
  user_id_max?: number | null
  cash_min?: number | string | null
  cash_max?: number | string | null
  debt_min?: number | string | null
  debt_max?: number | string | null
  is_active?: boolean | null
  include_superuser?: boolean
}

export interface BatchAdjustRequest {
  filter: BatchAdjustFilter
  amount: number | string
  reason: string
  dry_run: boolean
}

export interface BatchAdjustMatchedUser {
  id: number
  username: string
  cash_before: number
  debt: number
  cash_after: number
  will_fail: boolean
}

export interface BatchAdjustDryRunResponse {
  dry_run: true
  matched_count: number
  eligible_count: number
  will_fail_count: number
  total_delta: number
  matched_users: BatchAdjustMatchedUser[]
}

export interface BatchAdjustUpdatedItem {
  user_id: number
  username: string
  cash_before: number
  cash_after: number
}

export interface BatchAdjustFailedItem {
  user_id: number
  username: string
  reason: string
  cash_before: number
  would_be: number
}

export interface BatchAdjustExecuteResponse {
  dry_run: false
  updated_count: number
  failed_count: number
  total_delta: number
  updated: BatchAdjustUpdatedItem[]
  failed: BatchAdjustFailedItem[]
}

export type BatchAdjustResponse = BatchAdjustDryRunResponse | BatchAdjustExecuteResponse

export interface WealthBracket {
  label: string
  lower: number | null
  upper: number | null
  count: number
  sum_wealth: number
  pct: number
}

export interface WealthStats {
  user_count: number
  total_cash: number
  total_debt: number
  total_holdings_value: number
  total_net_worth: number
  mean: number
  median: number
  std: number
  min: number
  max: number
  p10: number
  p25: number
  p50: number
  p75: number
  p90: number
  p99: number
  gini: number
  brackets: WealthBracket[]
}

export const adminApi = {
  async listUsers(): Promise<UserListItem[]> {
    return api.get<UserListItem[]>('/api/v1/user/list')
  },

  async adjustCash(userId: number, amount: number, reason: string = ''): Promise<AdjustCashResult> {
    return api.post<AdjustCashResult>(`/api/v1/user/${userId}/adjust-cash`, { amount, reason })
  },

  async batchAdjustCash(req: BatchAdjustRequest): Promise<BatchAdjustResponse> {
    return api.post<BatchAdjustResponse>('/api/v1/user/batch-adjust-cash', req)
  },

  async wealthStats(): Promise<WealthStats> {
    return api.get<WealthStats>('/api/v1/admin/stats/wealth')
  },

  async setUserAdmin(userId: number, isAdmin: boolean): Promise<{ user_id: number; username: string; is_admin: boolean; changed: boolean }> {
    return api.patch<{ user_id: number; username: string; is_admin: boolean; changed: boolean }>(
      `/api/v1/user/${userId}/admin`,
      { is_admin: isAdmin },
    )
  },
}
