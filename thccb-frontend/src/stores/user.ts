import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { UserSummary, Holding, Transaction } from '@/types/api'
import { userApi } from '@/api/user'
import { useAuthStore } from '@/stores/auth'

export const useUserStore = defineStore('user', () => {
  // 状态
  const summary = ref<UserSummary | null>(null)
  const holdings = ref<Holding[]>([])
  const transactions = ref<Transaction[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  // 计算属性
  const totalHoldingsValue = computed(() => summary.value?.holdings_value ?? 0)

  const holdingsByMarket = computed(() => {
    const map = new Map<number, Holding[]>()
    holdings.value.forEach(holding => {
      if (!map.has(holding.market_id)) {
        map.set(holding.market_id, [])
      }
      map.get(holding.market_id)!.push(holding)
    })
    return map
  })

  const recentTransactions = computed(() => 
    transactions.value.slice(0, 10) // 最近10条交易记录
  )

  // Actions — manageLoading 参数控制是否由单个函数管理 loading 状态
  // 当被 fetchAllUserData 并发调用时传 false，避免 loading 在并发中反复切换
  const fetchSummary = async (manageLoading = true) => {
    const authStore = useAuthStore()
    if (!authStore.isAuthenticated) return null

    if (manageLoading) { loading.value = true; error.value = null }
    try {
      summary.value = await userApi.getSummary()
      return summary.value
    } catch (err: any) {
      error.value = err.message || '获取资产概览失败'
      console.error('获取资产概览失败:', err)
      return null
    } finally {
      if (manageLoading) loading.value = false
    }
  }

  const fetchHoldings = async (manageLoading = true) => {
    const authStore = useAuthStore()
    if (!authStore.isAuthenticated) return []

    if (manageLoading) { loading.value = true; error.value = null }
    try {
      holdings.value = await userApi.getHoldings()
      return holdings.value
    } catch (err: any) {
      error.value = err.message || '获取持仓明细失败'
      console.error('获取持仓明细失败:', err)
      return []
    } finally {
      if (manageLoading) loading.value = false
    }
  }

  const fetchTransactions = async (limit = 100, manageLoading = true) => {
    const authStore = useAuthStore()
    if (!authStore.isAuthenticated) return []

    if (manageLoading) { loading.value = true; error.value = null }
    try {
      transactions.value = await userApi.getTransactions(limit)
      return transactions.value
    } catch (err: any) {
      error.value = err.message || '获取交易历史失败'
      console.error('获取交易历史失败:', err)
      return []
    } finally {
      if (manageLoading) loading.value = false
    }
  }

  const fetchAllUserData = async () => {
    const authStore = useAuthStore()
    if (!authStore.isAuthenticated) {
      return { success: false, error: '用户未认证' }
    }

    loading.value = true
    error.value = null
    try {
      await Promise.all([
        fetchSummary(false),
        fetchHoldings(false),
        fetchTransactions(100, false)
      ])
      return { success: true }
    } catch (err: any) {
      error.value = err.message || '获取用户数据失败'
      console.error('获取用户数据失败:', err)
      return { success: false, error: error.value }
    } finally {
      loading.value = false
    }
  }

  const getHoldingByOutcome = (outcomeId: number) => {
    return holdings.value.find(h => h.outcome_id === outcomeId)
  }

  const getHoldingsByMarket = (marketId: number) => {
    return holdings.value.filter(h => h.market_id === marketId)
  }

  const clearData = () => {
    summary.value = null
    holdings.value = []
    transactions.value = []
    error.value = null
  }

  const clearError = () => {
    error.value = null
  }

  // 监听交易操作，更新持仓
  const updateAfterTrade = async (outcomeId: number, sharesChange: number) => {
    // 重新获取持仓数据
    await fetchHoldings()
    
    // 如果summary存在，也更新summary
    if (summary.value) {
      await fetchSummary()
    }
  }

  return {
    // 状态
    summary,
    holdings,
    transactions,
    loading,
    error,
    
    // 计算属性
    totalHoldingsValue,
    holdingsByMarket,
    recentTransactions,
    
    // Actions
    fetchSummary,
    fetchHoldings,
    fetchTransactions,
    fetchAllUserData,
    getHoldingByOutcome,
    getHoldingsByMarket,
    clearData,
    clearError,
    updateAfterTrade,
  }
})