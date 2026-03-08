import api from './index'
import type {
  UserSummary,
  Holding,
  Transaction,
  ApiResponse
} from '@/types/api'

export const userApi = {
  // 获取资产概览
  async getSummary(): Promise<UserSummary> {
    return api.get<UserSummary>('/api/v1/user/summary')
  },

  // 获取持仓明细
  async getHoldings(): Promise<Holding[]> {
    return api.get<Holding[]>('/api/v1/user/holdings')
  },

  // 获取交易历史
  async getTransactions(): Promise<Transaction[]> {
    return api.get<Transaction[]>('/api/v1/user/transactions')
  }
}

export default userApi