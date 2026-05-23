import api from './index'
import type { TitleRead } from './title'

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

  // ── 封号 / 解封 + Bot 预警审核 ────────────────────────────────────
  async banUser(userId: number, reason?: string, relatedSuspicionId?: number) {
    return api.patch<{ user_id: number; username: string; is_active: boolean; changed: boolean }>(
      `/api/v1/user/${userId}/ban`,
      { reason: reason || null, related_suspicion_id: relatedSuspicionId ?? null },
    )
  },

  async unbanUser(userId: number) {
    return api.patch<{ user_id: number; username: string; is_active: boolean; changed: boolean }>(
      `/api/v1/user/${userId}/unban`,
    )
  },

  async listBotSuspicions(status: 'pending' | 'reviewed' | 'all' = 'pending', limit: number = 100): Promise<BotSuspicionItem[]> {
    return api.get<BotSuspicionItem[]>('/api/v1/admin/bot/suspicions', { params: { status, limit } })
  },

  async reviewSuspicion(suspicionId: number, status: 'confirmed_bot' | 'false_positive' | 'pending', note?: string) {
    return api.patch(`/api/v1/admin/bot/suspicions/${suspicionId}/review`, { status, note: note || null })
  },

  async listBannedUsers(limit: number = 200): Promise<BannedUserItem[]> {
    return api.get<BannedUserItem[]>('/api/v1/admin/bot/banned-users', { params: { limit } })
  },

  async botStats(): Promise<BotStats> {
    return api.get<BotStats>('/api/v1/admin/bot/stats')
  },
}

export interface BotSuspicionItem {
  id: number
  user_id: number
  username: string
  user_is_active: boolean
  triggered_at: string
  signals: string
  metrics: string
  window_start: string
  window_end: string
  review_status: string
  reviewed_by: number | null
  reviewed_at: string | null
  review_note: string | null
}

export interface BannedUserItem {
  user_id: number
  username: string
  casdoor_id: string | null
}

export interface BotStats {
  pending_suspicions: number
  banned_users: number
}

// Title catalog admin (used by MarketManage's "required titles" picker; also reused in Tasks 21-23)
export interface TitleBatchItem {
  id: number
  title_id: number
  title_name: string
  name: string
  description: string
  total: number
  used: number
  created_at: string
}

export interface UserTitleItem {
  title: TitleRead
  granted_at: string
  source: string
  granted_by_admin_id: number | null
}

export interface AdminUserSummary {
  user_id: number
  username: string
  email: string
  cash: number
  debt: number
  is_active: boolean
  is_superuser: boolean
  equipped_title_id: number | null
}

export const adminTitleApi = {
  listTitles: () => api.get<TitleRead[]>('/api/v1/admin/titles'),
  createTitle: (body: { name: string; description?: string; color?: string; icon?: string; sort_order?: number }) =>
    api.post<TitleRead>('/api/v1/admin/titles', body),
  updateTitle: (id: number, body: Partial<TitleRead>) =>
    api.patch<TitleRead>(`/api/v1/admin/titles/${id}`, body),

  listBatches: () => api.get<TitleBatchItem[]>('/api/v1/admin/title-batches'),
  createBatch: (body: { title_id: number; name: string; description?: string }) =>
    api.post<TitleBatchItem>('/api/v1/admin/title-batches', body),
  importCodes: (batch_id: number, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return api.post<{ inserted: number }>(
      `/api/v1/admin/title-batches/${batch_id}/import-codes`,
      fd,
      { headers: { 'Content-Type': 'multipart/form-data' } },
    )
  },

  listUserTitles: (uid: number) => api.get<UserTitleItem[]>(`/api/v1/admin/users/${uid}/titles`),
  grantTitle: (uid: number, title_id: number) =>
    api.post(`/api/v1/admin/users/${uid}/titles`, { title_id }),
  revokeTitle: (uid: number, tid: number) =>
    api.delete(`/api/v1/admin/users/${uid}/titles/${tid}`),
  userSummary: (uid: number) => api.get<AdminUserSummary>(`/api/v1/admin/users/${uid}/summary`),

  getMarketRequired: (mid: number) =>
    api.get<number[]>(`/api/v1/admin/markets/${mid}/required-titles`),
  putMarketRequired: (mid: number, title_ids: number[]) =>
    api.put(`/api/v1/admin/markets/${mid}/required-titles`, { title_ids }),
}
