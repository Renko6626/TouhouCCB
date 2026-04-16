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

export const adminApi = {
  async listUsers(): Promise<UserListItem[]> {
    return api.get<UserListItem[]>('/api/v1/user/list')
  },

  async adjustCash(userId: number, amount: number, reason: string = ''): Promise<AdjustCashResult> {
    return api.post<AdjustCashResult>(`/api/v1/user/${userId}/adjust-cash`, { amount, reason })
  },
}
