import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { MarketListItem, MarketDetail, TradeResponse, QuoteResponse, MarketTrade, LeaderboardItem } from '@/types/api'
import { marketApi } from '@/api/market'

export const useMarketStore = defineStore('market', () => {
  // 状态
  const markets = ref<MarketListItem[]>([])
  const currentMarket = ref<MarketDetail | null>(null)
  const marketTrades = ref<MarketTrade[]>([])
  const leaderboard = ref<LeaderboardItem[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const tradeLoading = ref(false)
  const tradeError = ref<string | null>(null)

  // 计算属性
  const activeMarkets = computed(() => 
    markets.value.filter(m => m.status === 'trading')
  )
  
  const haltedMarkets = computed(() => 
    markets.value.filter(m => m.status === 'halt')
  )
  
  const settledMarkets = computed(() => 
    markets.value.filter(m => m.status === 'settled')
  )

  const currentMarketOutcomes = computed(() => 
    currentMarket.value?.outcomes || []
  )

  // Actions
  const fetchMarkets = async () => {
    loading.value = true
    error.value = null
    try {
      markets.value = await marketApi.getMarkets()
    } catch (err: any) {
      error.value = err.message || '获取市场列表失败'
      console.error('获取市场列表失败:', err)
    } finally {
      loading.value = false
    }
  }

  const fetchMarketDetail = async (marketId: number) => {
    loading.value = true
    error.value = null
    try {
      currentMarket.value = await marketApi.getMarketDetail(marketId)
    } catch (err: any) {
      error.value = err.message || '获取市场详情失败'
      console.error('获取市场详情失败:', err)
    } finally {
      loading.value = false
    }
  }

  const fetchMarketTrades = async (marketId: number, limit: number = 50) => {
    loading.value = true
    error.value = null
    try {
      marketTrades.value = await marketApi.getMarketTrades(marketId, limit)
    } catch (err: any) {
      error.value = err.message || '获取市场成交记录失败'
      console.error('获取市场成交记录失败:', err)
    } finally {
      loading.value = false
    }
  }

  const fetchLeaderboard = async (limit: number = 20) => {
    loading.value = true
    error.value = null
    try {
      leaderboard.value = await marketApi.getLeaderboard(limit)
    } catch (err: any) {
      error.value = err.message || '获取排行榜失败'
      console.error('获取排行榜失败:', err)
    } finally {
      loading.value = false
    }
  }

  const buyShares = async (outcomeId: number, shares: number) => {
    tradeLoading.value = true
    tradeError.value = null
    try {
      const result = await marketApi.buy(outcomeId, shares)
      
      // 如果当前有市场详情，重新获取以更新价格
      if (currentMarket.value) {
        await fetchMarketDetail(currentMarket.value.id)
      }
      
      return { success: true, data: result }
    } catch (err: any) {
      tradeError.value = err.message || '买入失败'
      console.error('买入失败:', err)
      return { success: false, error: tradeError.value }
    } finally {
      tradeLoading.value = false
    }
  }

  const sellShares = async (outcomeId: number, shares: number) => {
    tradeLoading.value = true
    tradeError.value = null
    try {
      const result = await marketApi.sell(outcomeId, shares)
      
      // 如果当前有市场详情，重新获取以更新价格
      if (currentMarket.value) {
        await fetchMarketDetail(currentMarket.value.id)
      }
      
      return { success: true, data: result }
    } catch (err: any) {
      tradeError.value = err.message || '卖出失败'
      console.error('卖出失败:', err)
      return { success: false, error: tradeError.value }
    } finally {
      tradeLoading.value = false
    }
  }

  const getQuote = async (outcomeId: number, shares: number, side: 'buy' | 'sell') => {
    try {
      const quote = await marketApi.quote({
        outcome_id: outcomeId,
        shares,
        side
      })
      return { success: true, data: quote }
    } catch (err: any) {
      console.error('获取报价失败:', err)
      return { success: false, error: err.message || '获取报价失败' }
    }
  }

  // 管理员操作
  const createMarket = async (title: string, description: string, liquidity_b: number, outcomes: string[]) => {
    loading.value = true
    error.value = null
    try {
      const result = await marketApi.createMarket(title, description, liquidity_b, outcomes)
      // 创建成功后重新获取市场列表
      await fetchMarkets()
      return { success: true, data: result }
    } catch (err: any) {
      error.value = err.message || '创建市场失败'
      console.error('创建市场失败:', err)
      return { success: false, error: error.value }
    } finally {
      loading.value = false
    }
  }

  const closeMarket = async (marketId: number) => {
    loading.value = true
    error.value = null
    try {
      const result = await marketApi.closeMarket(marketId)
      // 更新当前市场状态
      if (currentMarket.value?.id === marketId) {
        await fetchMarketDetail(marketId)
      }
      // 更新市场列表
      await fetchMarkets()
      return { success: true, data: result }
    } catch (err: any) {
      error.value = err.message || '关闭市场失败'
      console.error('关闭市场失败:', err)
      return { success: false, error: error.value }
    } finally {
      loading.value = false
    }
  }

  const resumeMarket = async (marketId: number) => {
    loading.value = true
    error.value = null
    try {
      const result = await marketApi.resumeMarket(marketId)
      // 更新当前市场状态
      if (currentMarket.value?.id === marketId) {
        await fetchMarketDetail(marketId)
      }
      // 更新市场列表
      await fetchMarkets()
      return { success: true, data: result }
    } catch (err: any) {
      error.value = err.message || '恢复市场失败'
      console.error('恢复市场失败:', err)
      return { success: false, error: error.value }
    } finally {
      loading.value = false
    }
  }

  const settleMarket = async (marketId: number, winningOutcomeId: number) => {
    loading.value = true
    error.value = null
    try {
      const result = await marketApi.settleMarket(marketId, winningOutcomeId)
      // 更新当前市场状态
      if (currentMarket.value?.id === marketId) {
        await fetchMarketDetail(marketId)
      }
      // 更新市场列表
      await fetchMarkets()
      return { success: true, data: result }
    } catch (err: any) {
      error.value = err.message || '结算市场失败'
      console.error('结算市场失败:', err)
      return { success: false, error: error.value }
    } finally {
      loading.value = false
    }
  }

  const clearCurrentMarket = () => {
    currentMarket.value = null
  }

  const clearError = () => {
    error.value = null
    tradeError.value = null
  }

  return {
    // 状态
    markets,
    currentMarket,
    marketTrades,
    leaderboard,
    loading,
    error,
    tradeLoading,
    tradeError,
    
    // 计算属性
    activeMarkets,
    haltedMarkets,
    settledMarkets,
    currentMarketOutcomes,
    
    // Actions
    fetchMarkets,
    fetchMarketDetail,
    fetchMarketTrades,
    fetchLeaderboard,
    buyShares,
    sellShares,
    getQuote,
    createMarket,
    closeMarket,
    resumeMarket,
    settleMarket,
    clearCurrentMarket,
    clearError,
  }
})