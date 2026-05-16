import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { MarketListItem, MarketDetail, MarketCreate, TradeResponse, QuoteResponse, MarketTrade, LeaderboardItem } from '@/types/api'
import type { TradeEventData } from '@/types/stream'
import { marketApi } from '@/api/market'

// SSE 增量更新时 marketTrades 列表的最大长度，超过则尾部截断防内存爆涨
const MAX_MARKET_TRADES = 100

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
  const fetchMarkets = async (params?: { keyword?: string; tag?: string; include_halt?: boolean; include_settled?: boolean }) => {
    loading.value = true
    error.value = null
    try {
      markets.value = await marketApi.getMarkets(params)
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

  // ── SSE 增量更新（perf）──
  // 收到 trade 事件时，直接用 payload 更新本地状态，避免触发 fetchMarketDetail
  // + fetchMarketTrades 两次全量 refetch（热门市场每笔成交会被放大 N×2 倍）。
  // 兜底机制由调用方（TradingView.vue）的低频 sanity refresh 提供，防 SSE 丢包 drift。
  const appendTradeFromSSE = (payload: TradeEventData['trade']) => {
    // 用 id 去重（SSE 重连或重发场景）
    if (marketTrades.value.some(t => t.id === payload.id)) return
    const trade: MarketTrade = {
      id: payload.id,
      outcome_id: payload.outcome_id,
      side: payload.type,
      shares: payload.shares,
      price: payload.price,
      gross: payload.gross,
      fee: payload.fee,
      timestamp: payload.timestamp,
      username: payload.username,
    }
    marketTrades.value.unshift(trade)
    if (marketTrades.value.length > MAX_MARKET_TRADES) {
      marketTrades.value = marketTrades.value.slice(0, MAX_MARKET_TRADES)
    }
  }

  // 用 trade.post_market_price 直接 patch 对应 outcome 的当前价。
  // 跳过 fetchMarketDetail 全量拉取。
  const patchOutcomePriceFromTrade = (outcomeId: number, newPrice: number) => {
    if (!currentMarket.value) return
    const outcome = currentMarket.value.outcomes.find(o => o.id === outcomeId)
    if (outcome) {
      outcome.current_price = newPrice
    }
  }

  // 用 trade.market_prices_post 全量 patch 所有 outcome 当前价（按 id 升序）。
  // 修复"LMSR 跨选项联动后其他 outcome 价格 stale"问题——不再只 patch 被交易那个。
  const patchAllPricesFromTrade = (marketPricesPost: number[]) => {
    if (!currentMarket.value) return
    const sorted = [...currentMarket.value.outcomes].sort((a, b) => a.id - b.id)
    if (sorted.length !== marketPricesPost.length) return  // 长度不匹配，跳过保护
    for (let i = 0; i < sorted.length; i++) {
      sorted[i]!.current_price = marketPricesPost[i]!
    }
  }

  const fetchLeaderboard = async (
    limit: number = 20,
    mode: 'net_worth' | 'spending' = 'net_worth',
  ) => {
    loading.value = true
    error.value = null
    try {
      leaderboard.value = await marketApi.getLeaderboard(limit, mode)
    } catch (err: any) {
      error.value = err.message || '获取排行榜失败'
      console.error('获取排行榜失败:', err)
    } finally {
      loading.value = false
    }
  }

  // 把后端滑点拒绝映射为用户友好提示
  const friendlySlippageMsg = (raw: string): string => {
    if (!raw) return raw
    if (raw.includes('滑点') || raw.includes('max_cost') || raw.includes('min_proceeds')) {
      return '市场波动较大，超过最大滑点，请刷新行情后重试'
    }
    return raw
  }

  const buyShares = async (outcomeId: number, shares: number, maxSlippageBps?: number, acceptAnySlippage: boolean = false) => {
    tradeLoading.value = true
    tradeError.value = null
    try {
      const result = await marketApi.buy(outcomeId, shares, maxSlippageBps, acceptAnySlippage)

      // 如果当前有市场详情，重新获取以更新价格
      if (currentMarket.value) {
        await fetchMarketDetail(currentMarket.value.id)
      }

      return { success: true, data: result }
    } catch (err: any) {
      tradeError.value = friendlySlippageMsg(err.message || '买入失败')
      console.error('买入失败:', err)
      return { success: false, error: tradeError.value }
    } finally {
      tradeLoading.value = false
    }
  }

  const sellShares = async (outcomeId: number, shares: number, maxSlippageBps?: number, acceptAnySlippage: boolean = false) => {
    tradeLoading.value = true
    tradeError.value = null
    try {
      const result = await marketApi.sell(outcomeId, shares, maxSlippageBps, acceptAnySlippage)

      // 如果当前有市场详情，重新获取以更新价格
      if (currentMarket.value) {
        await fetchMarketDetail(currentMarket.value.id)
      }

      return { success: true, data: result }
    } catch (err: any) {
      tradeError.value = friendlySlippageMsg(err.message || '卖出失败')
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
  const createMarket = async (data: MarketCreate) => {
    loading.value = true
    error.value = null
    try {
      const result = await marketApi.createMarket(data)
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

  const settleMarket = async (marketId: number, winningOutcomeId: number, payout: number = 1.0) => {
    loading.value = true
    error.value = null
    try {
      const result = await marketApi.resolveMarket(marketId, winningOutcomeId, payout)
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
    appendTradeFromSSE,
    patchOutcomePriceFromTrade,
    patchAllPricesFromTrade,
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