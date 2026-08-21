import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type {
  Holding, HoldingSlim, MarketPriceCtx, Transaction, UserSummary,
} from '@/types/api'
import { userApi } from '@/api/user'
import { marketApi } from '@/api/market'
import { useAuthStore } from '@/stores/auth'
import {
  applyFillToRows, computeHoldingsValueLcv, computeHoldingsValueMtm,
  enrichHolding, rankFromThresholds,
} from '@/utils/valuation'

export const useUserStore = defineStore('user', () => {
  const summary = ref<UserSummary | null>(null)
  const holdingsRaw = ref<HoldingSlim[]>([])
  const transactions = ref<Transaction[]>([])
  // 市场定价上下文：本地估值/预览的价格来源。fetchSummary 时全量重建，
  // 当前市场由 tick 帧经 patchMarketPrices 续写（TradingView 接线）。
  const priceContext = ref<Map<number, MarketPriceCtx>>(new Map())
  const loading = ref(false)
  const error = ref<string | null>(null)

  // ── 派生估值（原 /user/summary 服务端字段，阶段 3 起全部本地算） ──
  const totalCostBasis = computed(() =>
    (summary.value?.positions ?? []).reduce((s, p) => s + p.cost_basis, 0))
  const holdingsValueMtm = computed(() =>
    computeHoldingsValueMtm(summary.value?.positions ?? [], priceContext.value))
  const holdingsValueLcv = computed(() =>
    computeHoldingsValueLcv(summary.value?.positions ?? [], priceContext.value,
                            summary.value?.sell_fee_rate ?? 0))
  const netWorth = computed(() =>
    summary.value ? summary.value.cash - summary.value.debt + holdingsValueMtm.value : 0)
  const netWorthLcv = computed(() =>
    summary.value ? summary.value.cash - summary.value.debt + holdingsValueLcv.value : 0)
  const unrealizedPnl = computed(() => holdingsValueMtm.value - totalCostBasis.value)
  const unrealizedPnlLcv = computed(() => holdingsValueLcv.value - totalCostBasis.value)
  const rankTitle = computed(() =>
    rankFromThresholds(summary.value?.rank_thresholds ?? [], netWorth.value))
  // 显示用估算；权威 margin_status 仍来自 summary（服务端 LCV 口径）
  const marginRatioEstimate = computed<number | null>(() => {
    const s = summary.value
    if (!s || s.debt <= 0) return null
    return netWorthLcv.value / s.debt
  })

  // 派生持仓视图——字段名与旧 API Holding 一致，表格/持仓盒模板零改动
  const holdings = computed<Holding[]>(() => {
    const fee = summary.value?.sell_fee_rate ?? 0
    return holdingsRaw.value.map(h => enrichHolding(h, priceContext.value, fee))
  })

  const holdingsByMarket = computed(() => {
    const map = new Map<number, Holding[]>()
    holdings.value.forEach(h => {
      if (!map.has(h.market_id)) map.set(h.market_id, [])
      map.get(h.market_id)!.push(h)
    })
    return map
  })

  // ── priceContext 维护 ──
  const refreshPriceContext = async () => {
    const markets = await marketApi.getMarkets({ include_halt: true })
    const next = new Map<number, MarketPriceCtx>()
    for (const m of markets) {
      const sorted = [...m.outcomes].sort((a, b) => a.id - b.id)
      next.set(m.id, {
        b: m.liquidity_b,
        status: m.status,
        outcomeIds: sorted.map(o => o.id),
        prices: sorted.map(o => o.current_price),
      })
    }
    priceContext.value = next
  }

  /** tick 帧续写当前市场价格（prices 按 outcome.id 升序，与帧契约一致） */
  const patchMarketPrices = (marketId: number, prices: number[]) => {
    const ctx = priceContext.value.get(marketId)
    if (!ctx || ctx.prices.length !== prices.length) return
    ctx.prices = [...prices]
    priceContext.value = new Map(priceContext.value)  // 换引用触发 computed
  }

  // ── fetch actions（manageLoading 语义与旧版一致） ──
  const fetchSummary = async (manageLoading = true) => {
    const authStore = useAuthStore()
    if (!authStore.isAuthenticated) return null
    if (manageLoading) { loading.value = true; error.value = null }
    try {
      // priceContext 与 summary 并行刷新；价格上下文失败不阻断 summary
      const [s] = await Promise.all([
        userApi.getSummary(),
        refreshPriceContext().catch(err =>
          console.error('刷新价格上下文失败:', err)),
      ])
      summary.value = s
      return s
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
      holdingsRaw.value = await userApi.getHoldings()
      return holdingsRaw.value
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
    if (!authStore.isAuthenticated) return { success: false, error: '用户未认证' }
    loading.value = true
    error.value = null
    try {
      await Promise.all([
        fetchSummary(false),
        fetchHoldings(false),
        fetchTransactions(100, false),
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

  // ── 成交后本地 apply（spec §6.4：成交后不再调 summary/holdings） ──
  const applyTradeFill = (args: {
    side: 'buy' | 'sell'
    outcomeId: number
    marketId: number
    shares: number
    /** buy=实付 / sell=到手净额；调用方用 |旧 cash − new_cash| 推导（6dp 精确） */
    pay: number
    newCash: number
    outcomeLabel?: string
    marketTitle?: string
  }) => {
    const s = summary.value
    if (!s) return
    s.cash = args.newCash
    const fill = { side: args.side, outcomeId: args.outcomeId,
                   shares: args.shares, pay: args.pay }
    if (!applyFillToRows(s.positions, fill)) {
      s.positions.push({ outcome_id: args.outcomeId, market_id: args.marketId,
                         amount: args.shares, cost_basis: args.pay })
    }
    if (!applyFillToRows(holdingsRaw.value, fill)
        && args.outcomeLabel !== undefined && args.marketTitle !== undefined) {
      holdingsRaw.value.push({
        market_id: args.marketId, market_title: args.marketTitle,
        outcome_id: args.outcomeId, outcome_label: args.outcomeLabel,
        amount: args.shares, cost_basis: args.pay,
      })
    }
  }

  const getHoldingByOutcome = (outcomeId: number) =>
    holdings.value.find(h => h.outcome_id === outcomeId)

  const getHoldingsByMarket = (marketId: number) =>
    holdings.value.filter(h => h.market_id === marketId)

  const clearData = () => {
    summary.value = null
    holdingsRaw.value = []
    transactions.value = []
    priceContext.value = new Map()
    error.value = null
  }

  const clearError = () => { error.value = null }

  return {
    summary, holdings, transactions, loading, error, priceContext,

    totalCostBasis, holdingsValueMtm, holdingsValueLcv,
    netWorth, netWorthLcv, unrealizedPnl, unrealizedPnlLcv,
    rankTitle, marginRatioEstimate, holdingsByMarket,

    fetchSummary, fetchHoldings, fetchTransactions, fetchAllUserData,
    refreshPriceContext, patchMarketPrices, applyTradeFill,
    getHoldingByOutcome, getHoldingsByMarket, clearData, clearError,
  }
})
