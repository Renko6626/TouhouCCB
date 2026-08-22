<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, provide, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMarketStore } from '@/stores/market'
import { useUserStore } from '@/stores/user'
import { useAuthStore } from '@/stores/auth'
import { NButton, NCard, NSpin, NAlert, NTag, NEmpty, NModal, useMessage } from 'naive-ui'
import { useMarketRealtime, MarketRealtimeKey } from '@/composables/useMarketRealtime'
import TradePanel from '@/components/market/TradePanel.vue'
import MarketStatus from '@/components/market/MarketStatus.vue'
import OutcomeCard from '@/components/market/OutcomeCard.vue'
import PriceChart from '@/components/chart/PriceChart.vue'
import CandleChart from '@/components/chart/CandleChart.vue'
import MarginCallBanner from '@/components/market/MarginCallBanner.vue'
import TitleChip from '@/components/title/TitleChip.vue'
import type { ChartInterval, QuoteResponse, TradeLimit } from '@/types/api'
import { buyCost, sellProceeds } from '@/utils/lmsr'

const route = useRoute()
const router = useRouter()
const marketStore = useMarketStore()
const userStore = useUserStore()
const authStore = useAuthStore()
const message = useMessage()

// 状态
const loading = ref(false)
const marketId = computed(() => parseInt(route.params.id as string))
const marketIdForSSE = computed<number | null>(() =>
  Number.isFinite(marketId.value) ? marketId.value : null,
)

// SSE 实时流：useMarketRealtime 内部建立 EventSource、跟踪 seq、暴露 latestTrade /
// pricesByOutcome / gapToken 等反应式状态。通过 provide 注入给子组件（PriceChart /
// CandleChart）共享同一实例，避免重复建连接。
const realtime = useMarketRealtime(marketIdForSSE)
provide(MarketRealtimeKey, realtime)

// 交易表单
const tradeType = ref<'buy' | 'sell'>('buy')
const selectedOutcomeId = ref<number | null>(null)
const shares = ref(1)
// 最大滑点：默认 1%（100 bps）；服务端 hardcap=1000 bps
const maxSlippageBps = ref(100)
const activeChartType = ref<'price' | 'candle'>('candle')
const candleInterval = ref<ChartInterval>('1m')
const candleIntervalOptions = [
  { label: '10秒', value: '10s' },
  { label: '1分钟', value: '1m' },
  { label: '15分钟', value: '15m' },
  { label: '1小时', value: '1h' },
] as const

let realtimeRefreshTimer: ReturnType<typeof setTimeout> | null = null

// 手机端悬浮交易按钮：面板可见时自动隐藏
const tradePanelRef = ref<HTMLElement | null>(null)
const tradePanelVisible = ref(false)
let tradePanelObserver: IntersectionObserver | null = null

const scrollToTradePanel = () => {
  tradePanelRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

// 加载市场数据
const loadError = ref('')
const loadMarketData = async () => {
  if (!marketId.value) return

  loading.value = true
  loadError.value = ''
  try {
    await Promise.all([
      marketStore.fetchMarketDetail(marketId.value),
      marketStore.fetchMarketTrades(marketId.value)
    ])
    // 默认选择第一个选项
    if (marketStore.currentMarket?.outcomes?.length) {
      selectedOutcomeId.value = marketStore.currentMarket.outcomes[0]?.id ?? null
    }
    // 两项都完成但 currentMarket 仍缺失 → 网络/接口故障
    if (!marketStore.currentMarket && marketStore.error) {
      loadError.value = marketStore.error
    }
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : '情报丢失在结界中，请重试'
  } finally {
    loading.value = false
  }
}

// 加载用户资产（并行请求）
const loadUserData = async () => {
  if (authStore.isAuthenticated) {
    await Promise.all([
      userStore.fetchSummary(),
      userStore.fetchHoldings()
    ])
  }
}

// 称号准入门槛 — 不达标用户首次进入市场弹窗引导到兑换页
const showGateModal = ref(false)
function goToRedeem() {
  showGateModal.value = false
  router.push('/redeem-title')
}
watch(
  () => [
    marketStore.currentMarket?.id,
    marketStore.currentMarket?.user_can_trade,
    marketStore.currentMarket?.required_titles?.length,
  ],
  ([, canTrade, requiredCount]) => {
    if (requiredCount && !canTrade && authStore.isAuthenticated) {
      showGateModal.value = true
    }
  },
  { immediate: false },
)

// 初始化加载（SSE 连接由 useMarketRealtime 内部 watch(marketIdForSSE) 自动建立）
onMounted(() => {
  loadMarketData()
  loadUserData()
})

// 交易面板可见性观察：挂载后按 ref 启用
watch(tradePanelRef, (el) => {
  if (tradePanelObserver) {
    tradePanelObserver.disconnect()
    tradePanelObserver = null
  }
  if (el && typeof IntersectionObserver !== 'undefined') {
    tradePanelObserver = new IntersectionObserver(
      ([entry]) => { tradePanelVisible.value = entry!.isIntersecting },
      { threshold: 0.15 }
    )
    tradePanelObserver.observe(el)
  }
})

onBeforeUnmount(() => {
  // useMarketRealtime 自己有 onBeforeUnmount disconnect
  if (realtimeRefreshTimer) {
    clearTimeout(realtimeRefreshTimer)
    realtimeRefreshTimer = null
  }
  if (tradePanelObserver) {
    tradePanelObserver.disconnect()
    tradePanelObserver = null
  }
})

// 监听市场 ID 变化：SSE 重连由 useMarketRealtime 内部 watch 完成；这里只重拉市场数据
watch(marketId, () => {
  loadMarketData()
})

const scheduleRealtimeRefresh = () => {
  if (realtimeRefreshTimer) return
  realtimeRefreshTimer = setTimeout(async () => {
    realtimeRefreshTimer = null
    if (!marketId.value) return
    await Promise.all([
      marketStore.fetchMarketDetail(marketId.value),
      marketStore.fetchMarketTrades(marketId.value, 30),
    ])
  }, 300)
}

// ── SSE 驱动的本地状态更新（不再 refetch）──
// 每条 trade → 增量 append 到 marketTrades + 用 market_prices_post 全量 patch outcomes 当前价
watch(realtime.latestTrade, (trade) => {
  if (!trade) return
  // 不再跳过 tradeLoading 窗口：成交后已无 refetch 可冲突（阶段 3 本地 apply），
  // 自己那笔的事件靠 appendTradeFromSSE 的 trade.id 去重；跳过反而会把自己
  // 下单引发的价格变动丢掉，安静市场上要等下一笔别人交易才能纠正。
  marketStore.appendTradeFromSSE(trade)
  if (trade.market_prices_post) {
    marketStore.patchAllPricesFromTrade(trade.market_prices_post)
  } else {
    // 老后端没带 market_prices_post → 退化只 patch 被交易那个
    marketStore.patchOutcomePriceFromTrade(trade.outcome_id, trade.post_market_price)
  }
})

// ── tick 帧驱动的本地状态更新（阶段 2）──
// 一帧可含多笔成交，逐笔 append；帧价格向量整体 patch（服务端 8dp 权威值）。
// 老 latestTrade watcher 保留作为「新前端+老后端」部署窗口的降级路径：
// tickSeen 后 latestTrade 不再更新，它自然沉默。
watch(realtime.latestTick, (frame) => {
  if (!frame) return
  // 同上：tradeLoading 窗口内的帧不能丢（自己下单的帧常在响应返回前后 125ms 内
  // 到达），trade.id 去重保证不会双记
  for (const t of frame.trades) {
    marketStore.appendTradeFromSSE(t)
  }
  if (frame.prices.length) {
    marketStore.patchAllPricesFromTrade(frame.prices)   // 帧价格与 market_prices_post 同序（id 升序）
    // 本地估值（Portfolio/Home/TradePanel 的 holdings/net_worth）用的 priceContext
    // 只由 summary 全量重建，这里续写当前市场，保持数字不陈旧。
    userStore.patchMarketPrices(marketId.value, frame.prices)
  }
})

// market_status 变化（halt/settled）→ 重拉 market detail 让 UI 状态机更新
watch(realtime.latestMarketStatus, (status) => {
  if (!status) return
  // 不跳过 tradeLoading 窗口：latestMarketStatus 只在状态变化时更新一次，
  // 此处 return 会让该次变化永久丢失（watcher 不会为同值重触发）
  scheduleRealtimeRefresh()
})

// SSE seq gap 检测触发的 reconcile：拉一次成交列表覆盖断线期间漏的；
// summary + holdings 一起刷（断线期间可能被强平，只刷 summary 会让 holdings 陈旧，审计 M10）
watch(realtime.gapToken, () => {
  if (realtime.gapToken.value > 0 && marketId.value) {
    marketStore.fetchMarketTrades(marketId.value, 50).catch(() => {})
    userStore.fetchSummary(false).catch(() => {})
    userStore.fetchHoldings(false).catch(() => {})
  }
})

// snapshot（首连/重连）重锚定：把 snapshot 价格回写 marketStore / priceContext。
// 否则重连后到下一笔成交之前，报价用 pricesByOutcome 的新价，而 OutcomeCard /
// 预估滑点 / maxShares 用 currentMarket 的旧价——同一页两套价格（审计 M8）
watch(realtime.snapshotToken, () => {
  if (!marketId.value || !marketStore.currentMarket) return
  const order = realtime.outcomesOrder.value
  if (!order.length) return
  const prices: number[] = []
  for (const oid of order) {
    const p = realtime.pricesByOutcome.value.get(oid)
    if (p === undefined) return
    prices.push(p)
  }
  marketStore.patchAllPricesFromTrade(prices)
  userStore.patchMarketPrices(marketId.value, prices)
})

const realtimeStatusType = computed<'success' | 'warning'>(() => {
  return realtime.isConnected.value ? 'success' : 'warning'
})

const realtimeStatusText = computed(() => {
  if (realtime.isConnected.value) return '实时连接中'
  if (realtime.reconnectCount.value > 0) return `重连中 (${realtime.reconnectCount.value})`
  return '未连接'
})

// 获取当前选择的选项
const selectedOutcome = computed(() => {
  if (!selectedOutcomeId.value || !marketStore.currentMarket?.outcomes) return null
  return marketStore.currentMarket.outcomes.find((o: any) => o.id === selectedOutcomeId.value)
})

// 获取用户在该选项的持仓
const userHolding = computed(() => {
  if (!selectedOutcomeId.value || !authStore.isAuthenticated) return null
  return userStore.getHoldingByOutcome(selectedOutcomeId.value) ?? null
})

// 计算最大可交易份额
// 买入时：LMSR 非线性定价导致买入越多单价越高，线性估算会高估，
// 所以施加保守折扣（取 70% 的线性估算值）；实际可行性以 quote 报价为准。
const maxShares = computed(() => {
  if (tradeType.value === 'sell') {
    return userHolding.value?.amount || 0
  }

  if (!selectedOutcome.value || !userStore.summary) return 0

  const cash = userStore.summary.cash
  const price = selectedOutcome.value.current_price
  if (price <= 0) return 0
  // 保守估算：LMSR 滑点使实际成本高于 线性(price * shares)，取 70% 避免超支
  return Math.max(1, Math.floor((cash / price) * 0.7))
})

// 报价超出可用现金时标记为不可交易
const quoteExceedsCash = computed(() => {
  if (tradeType.value !== 'buy' || !quoteResult.value || !userStore.summary) return false
  return quoteResult.value.net > userStore.summary.cash
})

// 本地报价预览（spec §6.1/§6.3）：闭式公式 + 当前价 + b + sell_fee_rate。
// 永远是预览，成交以 writer 返回为准；滑点保护仍由服务端执行。
const quoteResult = computed<QuoteResponse | null>(() => {
  const oid = selectedOutcomeId.value
  const mkt = marketStore.currentMarket
  const n = shares.value
  if (!oid || !mkt || n <= 0) return null
  const outcome = mkt.outcomes.find(o => o.id === oid)
  if (!outcome) return null
  // 优先 SSE 实时价（tick 帧续写），回退 REST 详情价
  const p = realtime.pricesByOutcome.value.get(oid) ?? outcome.current_price
  if (p <= 0) return null
  const b = mkt.liquidity_b
  if (tradeType.value === 'buy') {
    const gross = buyCost(p, n, b)
    if (gross <= 0) return null
    return { outcome_id: oid, side: 'buy', shares: n,
             avg_price: gross / n, gross, fee: 0, net: gross }
  }
  const gross = sellProceeds(p, n, b)
  if (gross <= 0) return null
  const feeRate = userStore.summary?.sell_fee_rate ?? 0
  const fee = gross * feeRate
  return { outcome_id: oid, side: 'sell', shares: n,
           avg_price: gross / n, gross, fee, net: gross - fee }
})

// 计算交易后现金
const estimatedNewCash = computed(() => {
  if (!quoteResult.value || !userStore.summary) return 0
  const netCost = quoteResult.value.net || 0
  return tradeType.value === 'buy' 
    ? userStore.summary.cash - netCost
    : userStore.summary.cash + Math.abs(netCost)
})

// 执行交易：成交后本地 apply（spec §6.4），不再 refetch 市场/用户数据。
// pay 用后端返回的 6dp 精确成交额（不再从本地现金差推导——prevCash 是缓存，
// 多标签 / bot / 借还款 / 强平任一存在都会把外部现金变动算进 cost_basis）。
//
// 价格保护：用户设了滑点上限时，把本地预览 net 换算成绝对 max_cost / min_proceeds
// 一并发给后端。后端 bps 检查是相对「执行时刻边际价」的，预览价与成交价之间
// 没有保护——行情断线/在途期间别人拉价 8%，bps=100 仍会通过。绝对限额让
// 「所见即所得」成立；「无限制」档两者都不发。
const executeTrade = async () => {
  if (!selectedOutcomeId.value || shares.value <= 0) return

  const acceptAnySlippage = maxSlippageBps.value === -1
  const effectiveBps = acceptAnySlippage ? undefined : maxSlippageBps.value
  const prevCash = userStore.summary?.cash ?? null

  let limit: TradeLimit | undefined
  const preview = quoteResult.value
  if (!acceptAnySlippage && effectiveBps !== undefined && preview) {
    const tol = effectiveBps / 10000
    limit = tradeType.value === 'buy'
      ? { max_cost: Math.ceil(preview.net * (1 + tol) * 1e6) / 1e6 }
      : { min_proceeds: Math.floor(preview.net * (1 - tol) * 1e6) / 1e6 }
  }

  try {
    const result = tradeType.value === 'buy'
      ? await marketStore.buyShares(selectedOutcomeId.value, shares.value, effectiveBps, acceptAnySlippage, limit)
      : await marketStore.sellShares(selectedOutcomeId.value, shares.value, effectiveBps, acceptAnySlippage, limit)

    if (!result.success) {
      message.error(result.error || '交易失败，请重试')
      return
    }
    message.success(`${tradeType.value === 'buy' ? '买入' : '卖出'}成功`)

    if (result.data && prevCash !== null && marketStore.currentMarket) {
      userStore.applyTradeFill({
        side: tradeType.value,
        outcomeId: selectedOutcomeId.value,
        marketId: marketStore.currentMarket.id,
        shares: result.data.shares,
        pay: result.data.pay ?? Math.abs(prevCash - result.data.new_cash),
        newCash: result.data.new_cash,
        outcomeLabel: selectedOutcome.value?.label,
        marketTitle: marketStore.currentMarket.title,
      })
    }
    // 份数不重置：连续加仓/分批减仓是常规用法。卖出后持仓不足时
    // TradePanel 的 shares > maxShares 门会禁用按钮，不会误下单。
  } catch (err: any) {
    message.error(err?.message || '交易失败，请重试')
  }
}

// ── 最近成交辅助 ──
const truncate = (s: string, n: number) => s && s.length > n ? s.slice(0, n - 1) + '…' : (s || '')

const outcomeLabel = (oid: number): string => {
  return marketStore.currentMarket?.outcomes?.find((o: any) => o.id === oid)?.label || '未知选项'
}

const relTime = (iso: string): string => {
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const diff = Math.max(0, Math.floor((Date.now() - t) / 1000))
  if (diff < 60) return `${diff}秒前`
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

</script>

<template>
  <div class="trading-view-page">
    <!-- ── 保证金警告横幅（warning/danger 时可见） ── -->
    <MarginCallBanner />

    <!-- 加载状态 -->
    <div v-if="loading" class="text-center py-12">
      <NSpin size="large" />
      <p class="mt-4 text-black">正在加载市场…</p>
    </div>

    <!-- 交易界面 -->
    <div v-else-if="marketStore.currentMarket" class="space-y-6">
      <!-- ── 称号准入门槛横幅（Task 11/13/19） ── -->
      <div
        v-if="marketStore.currentMarket.required_titles?.length && !marketStore.currentMarket.user_can_trade"
        class="title-gate-banner"
      >
        <div class="title-gate-banner-main">
          <span class="title-gate-icon" aria-hidden="true">⚠️</span>
          <strong class="title-gate-strong">此市场仅限以下称号交易：</strong>
          <TitleChip
            v-for="t in marketStore.currentMarket.required_titles"
            :key="t.id"
            :title="t"
            size="sm"
          />
        </div>
        <p class="title-gate-sub">
          你可继续浏览价格与图表，但下单会被拒绝。
          <a class="title-gate-link" @click="showGateModal = true">查看详情</a>
        </p>
      </div>

      <!-- 市场概要栏 -->
      <div class="market-summary-bar">
        <div class="summary-main">
          <h2 class="summary-title">{{ marketStore.currentMarket.title }}</h2>
          <p class="summary-desc">{{ marketStore.currentMarket.description }}</p>
        </div>
        <div class="summary-meta">
          <div class="summary-item">
            <span class="summary-label">流动性</span>
            <span class="summary-value">金 {{ marketStore.currentMarket.liquidity_b.toLocaleString() }}</span>
          </div>
          <div class="summary-item">
            <span class="summary-label">选中选项</span>
            <span class="summary-value">{{ selectedOutcome?.label || '未选择' }}</span>
          </div>
          <div class="summary-item">
            <span class="summary-label">当前价格</span>
            <span class="summary-value">
              {{ selectedOutcome?.current_price?.toFixed(4) || '—' }}
              <span
                v-if="selectedOutcome?.price_change_pct_24h != null"
                :style="{ color: selectedOutcome.price_change_pct_24h > 0 ? 'var(--color-up)' : selectedOutcome.price_change_pct_24h < 0 ? 'var(--color-down)' : '#888', fontSize: '12px', marginLeft: '6px', fontWeight: '700' }"
              >
                {{ selectedOutcome.price_change_pct_24h > 0 ? '+' : '' }}{{ selectedOutcome.price_change_pct_24h.toFixed(1) }}%
              </span>
            </span>
          </div>
          <div class="summary-item">
            <span class="summary-label">实时推送</span>
            <span :class="['realtime-dot', realtime.isConnected.value ? 'dot-on' : 'dot-off']">
              {{ realtimeStatusText }}
            </span>
          </div>
        </div>
      </div>

      <!--
        布局：手机端按 DOM 顺序单列堆叠 → 图表 → 选项 → 交易面板 → 最近成交。
        桌面 xl+：两列网格，交易面板固定在右侧并跨三行 sticky。
      -->
      <div class="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
        <!-- 1. 图表 (手机 row1 / 桌面 col1 row1) -->
        <NCard class="xl:col-start-1 xl:row-start-1">
          <template #header>
            <div class="flex items-center justify-between gap-4">
              <div>
                <span class="text-xs font-semibold uppercase tracking-[0.12em] text-[#888]">主视图</span>
                <h2 class="mt-1 text-lg font-bold text-black">
                  {{ activeChartType === 'candle' ? 'K线图' : '价格走势' }}
                  <span class="text-sm font-normal text-[#555] ml-2">{{ selectedOutcome?.label || '请先选择选项' }}</span>
                </h2>
              </div>
              <div class="flex items-center gap-2 flex-wrap">
                <NButton size="small" :type="activeChartType === 'price' ? 'primary' : 'default'" @click="activeChartType = 'price'">价格走势</NButton>
                <NButton size="small" :type="activeChartType === 'candle' ? 'primary' : 'default'" @click="activeChartType = 'candle'">K线图</NButton>
                <span class="text-xs text-[#888] ml-2">|</span>
                <NButton
                  v-for="opt in candleIntervalOptions"
                  :key="opt.value"
                  size="tiny"
                  :type="candleInterval === opt.value ? 'primary' : 'default'"
                  @click="candleInterval = opt.value"
                >{{ opt.label }}</NButton>
              </div>
            </div>
          </template>

          <div class="h-[300px] sm:h-[400px] md:h-[560px]">
            <PriceChart
              v-if="activeChartType === 'price' && selectedOutcomeId && marketStore.currentMarket"
              :outcome-id="selectedOutcomeId"
              :interval="candleInterval"
              height="100%"
            />
            <CandleChart
              v-else-if="selectedOutcomeId && marketStore.currentMarket"
              :outcome-id="selectedOutcomeId"
              :interval="candleInterval"
              height="100%"
            />
          </div>
        </NCard>

        <!-- 2. 市场选项 (手机 row2 / 桌面 col1 row2) -->
        <NCard class="xl:col-start-1 xl:row-start-2">
          <template #header>
            <div class="flex items-center justify-between gap-4">
              <span class="font-bold text-black">市场选项</span>
              <MarketStatus :status="marketStore.currentMarket.status" />
            </div>
          </template>

          <div class="space-y-3">
            <OutcomeCard
              v-for="outcome in marketStore.currentMarket.outcomes"
              :key="outcome.id"
              :outcome="outcome"
              :is-selected="selectedOutcomeId === outcome.id"
              @click="selectedOutcomeId = outcome.id"
            />
          </div>
        </NCard>

        <!-- 3. 交易面板 (手机 row3，紧跟选项；桌面 col2 row1 sticky 跨3行) -->
        <div
          ref="tradePanelRef"
          class="space-y-4 self-start xl:col-start-2 xl:row-start-1 xl:row-span-3 xl:sticky xl:top-6"
        >
          <TradePanel
            :market="marketStore.currentMarket"
            :selected-outcome-id="selectedOutcomeId"
            :trade-type="tradeType"
            :shares="shares"
            :max-shares="maxShares"
            :quote-result="quoteResult"
            :estimated-new-cash="estimatedNewCash"
            :user-holding="userHolding"
            :quote-exceeds-cash="quoteExceedsCash"
            :max-slippage-bps="maxSlippageBps"
            :user-can-trade="marketStore.currentMarket.user_can_trade ?? true"
            :realtime-connected="realtime.isConnected.value"
            @update:selected-outcome-id="selectedOutcomeId = $event"
            @update:trade-type="tradeType = $event"
            @update:shares="shares = $event"
            @update:max-slippage-bps="maxSlippageBps = $event"
            @execute-trade="executeTrade"
          />
        </div>

        <!-- 4. 最近成交 (手机 row4 / 桌面 col1 row3) -->
        <NCard
          v-if="marketStore.marketTrades.length"
          title="最近成交"
          class="xl:col-start-1 xl:row-start-3"
        >
          <ul class="trades-list">
            <TransitionGroup name="trade-flip">
              <li
                v-for="trade in marketStore.marketTrades.slice(0, 30)"
                :key="trade.id"
                class="trade-row"
                :class="trade.side === 'buy' ? 'trade-row-buy' : 'trade-row-sell'"
              >
                <span class="trade-time">{{ relTime(trade.timestamp) }}</span>
                <span class="trade-user" :title="trade.username">{{ truncate(trade.username, 12) }}</span>
                <span class="trade-action">
                  <span :class="['trade-tag', trade.side === 'buy' ? 'trade-tag-buy' : 'trade-tag-sell']">
                    {{ trade.side === 'buy' ? '买入' : '卖出' }}
                  </span>
                </span>
                <span class="trade-outcome" :title="outcomeLabel(trade.outcome_id)">
                  {{ truncate(outcomeLabel(trade.outcome_id), 10) }}
                </span>
                <span class="trade-shares">{{ Number(trade.shares).toLocaleString() }} 份</span>
                <span class="trade-price">@ 金 {{ Number(trade.price).toFixed(4) }}</span>
                <span class="trade-gross">金 {{ Number(trade.gross).toFixed(2) }}</span>
              </li>
            </TransitionGroup>
          </ul>
        </NCard>
      </div>
    </div>

    <!-- 手机端悬浮交易按钮（xl 以下显示，面板可见时自动隐藏） -->
    <button
      v-if="marketStore.currentMarket && !tradePanelVisible"
      type="button"
      class="mobile-trade-fab xl:hidden"
      :class="tradeType === 'buy' ? 'fab-buy' : 'fab-sell'"
      aria-label="滚动到交易面板"
      @click="scrollToTradePanel"
    >
      <span class="fab-label">{{ tradeType === 'buy' ? '买入' : '卖出' }}</span>
      <span class="fab-arrow" aria-hidden="true">↓</span>
    </button>

    <!-- 加载失败（网络/接口故障，和真 404 区分）-->
    <div v-if="!loading && !marketStore.currentMarket && loadError" class="text-center py-12">
      <NAlert type="error" :title="loadError">
        <div style="margin-top:8px; display:flex; gap:8px; justify-content:center">
          <NButton size="small" @click="loadMarketData">重新加载</NButton>
          <NButton size="small" @click="router.push('/market/list')">返回列表</NButton>
        </div>
      </NAlert>
    </div>

    <!-- 市场不存在（显式条件：避免 v-else 错配到上方 FAB 的 v-if 链） -->
    <div v-else-if="!loading && !marketStore.currentMarket" class="text-center py-12">
      <NEmpty description="市场不存在或已被删除">
        <template #extra>
          <NButton type="primary" @click="router.push('/market/list')">
            返回市场列表
          </NButton>
        </template>
      </NEmpty>
    </div>

    <!-- ── 称号门槛弹窗（首次进入不达标市场自动弹出，引导兑换） ── -->
    <NModal v-model:show="showGateModal" :mask-closable="false">
      <div class="gate-modal">
        <div class="gate-modal-header">
          <span class="gate-modal-icon">⚠️</span>
          <span class="gate-modal-title">需要特定称号</span>
        </div>
        <div class="gate-modal-body">
          <p class="gate-modal-main">
            此市场仅限<strong>持有以下称号的用户</strong>下单交易：
          </p>
          <div class="gate-modal-titles">
            <TitleChip
              v-for="t in marketStore.currentMarket?.required_titles ?? []"
              :key="t.id"
              :title="t"
            />
          </div>
          <p class="gate-modal-sub">
            你<strong>尚未拥有</strong>任何一项所需称号。可使用管理员发放的<strong>激活码</strong>兑换。
          </p>
          <p class="gate-modal-hint">
            仍可浏览价格 / 图表，但买入下单会被拒绝；如果已持有该市场的份额，卖出仍然可用。
          </p>
        </div>
        <div class="gate-modal-footer">
          <NButton @click="showGateModal = false">继续浏览</NButton>
          <NButton type="primary" @click="goToRedeem">去兑换激活码 →</NButton>
        </div>
      </div>
    </NModal>
  </div>
</template>

<style scoped>
.trading-view-page {
  max-width: 1400px;
  margin: 0 auto;
}

/* ── 称号准入门槛横幅 ── */
.title-gate-banner {
  border: 4px solid #f5a623;
  background: #fff8e1;
  padding: 12px 16px;
  margin-bottom: 16px;
  box-shadow: 4px 4px 0 #000;
}

.title-gate-banner-main {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.title-gate-icon {
  font-size: 18px;
  line-height: 1;
}

.title-gate-strong {
  font-size: 13px;
  font-weight: 800;
  color: #000;
  letter-spacing: 0.04em;
}

.title-gate-sub {
  margin: 8px 0 0;
  font-size: 12px;
  color: #555;
  line-height: 1.5;
}

.title-gate-link {
  margin-left: 6px;
  color: #000;
  text-decoration: underline;
  cursor: pointer;
  font-weight: 700;
}
.title-gate-link:hover {
  color: #b25c00;
}

/* ── 称号门槛弹窗 ── */
.gate-modal {
  background: #fff;
  border: 4px solid #f5a623;
  box-shadow: 8px 8px 0 #000;
  width: 480px;
  max-width: calc(100vw - 32px);
}

.gate-modal-header {
  background: #f5a623;
  color: #000;
  padding: 14px 20px;
  display: flex;
  align-items: center;
  gap: 12px;
  border-bottom: 4px solid #000;
}

.gate-modal-icon {
  font-size: 28px;
  line-height: 1;
}

.gate-modal-title {
  font-size: 20px;
  font-weight: 900;
  letter-spacing: 0.05em;
}

.gate-modal-body {
  padding: 18px 20px;
}

.gate-modal-main {
  font-size: 14px;
  font-weight: 700;
  color: #000;
  margin: 0 0 12px;
  line-height: 1.5;
}

.gate-modal-titles {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 8px 10px;
  border: 2px solid #000;
  background: #fafafa;
  margin-bottom: 14px;
}

.gate-modal-sub {
  font-size: 13px;
  color: #333;
  margin: 0 0 8px;
  line-height: 1.5;
}

.gate-modal-hint {
  font-size: 12px;
  color: #666;
  margin: 0;
  padding: 8px 10px;
  background: #f5f5f5;
  border-left: 4px solid #888;
  line-height: 1.5;
}

.gate-modal-footer {
  padding: 12px 20px 16px;
  border-top: 2px solid #000;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

/* ── 最近成交列表 ── */
.trades-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 360px;
  overflow-y: auto;
}

.trade-row {
  display: grid;
  grid-template-columns: 64px 84px 52px 88px 76px 90px auto;
  gap: 10px;
  align-items: center;
  padding: 7px 10px;
  border-bottom: 1px solid #f0f0f0;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.trade-row:last-child { border-bottom: none; }
.trade-row-buy { border-left: 3px solid var(--color-up); }
.trade-row-sell { border-left: 3px solid var(--color-down); }

.trade-time { font-size: 11px; color: #888; white-space: nowrap; }

.trade-user {
  font-weight: 600;
  color: #000;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trade-action { display: flex; align-items: center; }

.trade-tag {
  display: inline-block;
  padding: 1px 6px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  border: 1px solid #000;
}

.trade-tag-buy {
  background: var(--color-up);
  color: #fff;
  border-color: var(--color-up);
}

.trade-tag-sell {
  background: var(--color-down);
  color: #fff;
  border-color: var(--color-down);
}

.trade-outcome {
  color: #333;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trade-shares {
  font-weight: 600;
  color: #000;
  text-align: right;
  white-space: nowrap;
}

.trade-price {
  color: #555;
  white-space: nowrap;
}

.trade-gross {
  font-weight: 700;
  color: #000;
  text-align: right;
  white-space: nowrap;
}

/* 滚入动画 */
.trade-flip-enter-active {
  transition: transform 0.22s ease, opacity 0.22s ease;
}
.trade-flip-leave-active {
  transition: opacity 0.14s ease;
  position: absolute;
}
.trade-flip-enter-from {
  opacity: 0;
  transform: translateY(-10px);
}
.trade-flip-leave-to { opacity: 0; }
.trade-flip-move { transition: transform 0.22s ease; }

/* 窄屏：降到单列列信息，隐藏价格/总额之一 */
@media (max-width: 1024px) {
  .trade-row {
    grid-template-columns: 60px 72px 48px 76px 64px auto;
    gap: 8px;
    font-size: 11px;
  }
  .trade-gross { display: none; }
}

@media (max-width: 640px) {
  .trade-row {
    grid-template-columns: 56px 64px 44px 1fr auto;
  }
  .trade-outcome { display: none; }
  .trade-price { display: none; }
  .trade-gross { display: inline; text-align: right; }
}

/* 市场概要栏 */
.market-summary-bar {
  display: flex;
  gap: 24px;
  align-items: flex-start;
  padding: 14px 18px;
  border: 2px solid #000000;
  background: #000000;
  color: #ffffff;
  margin-bottom: 20px;
  flex-wrap: wrap;
}

.summary-main {
  flex: 1;
  min-width: 200px;
}

.summary-title {
  font-size: 16px;
  font-weight: 700;
  color: #ffffff;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-bottom: 4px;
}

.summary-desc {
  font-size: 12px;
  color: rgba(255,255,255,0.6);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
}

.summary-meta {
  display: flex;
  gap: 24px;
  flex-shrink: 0;
  flex-wrap: wrap;
}

.summary-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.summary-label {
  font-size: 10px;
  font-weight: 600;
  color: rgba(255,255,255,0.5);
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.summary-value {
  font-size: 14px;
  font-weight: 600;
  color: #ffffff;
  font-variant-numeric: tabular-nums;
}

/* 实时状态指示 */
.realtime-dot {
  font-size: 12px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 5px;
}

.realtime-dot::before {
  content: '';
  width: 7px;
  height: 7px;
  border-radius: 50%;
  display: inline-block;
  flex-shrink: 0;
}

.dot-on {
  color: #aaffaa;
}

.dot-on::before {
  background: #44ff44;
  box-shadow: 0 0 6px #44ff44;
  animation: blink 2s ease-in-out infinite;
}

.dot-off {
  color: rgba(255,255,255,0.4);
}

.dot-off::before {
  background: #888888;
}

@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

/* 移动端响应式 */
@media (max-width: 640px) {
  .trading-view-page {
    padding: 0 4px;
  }

  .market-summary-bar {
    gap: 12px;
    padding: 12px 14px;
  }

  .summary-title {
    font-size: 15px;
    white-space: normal;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }

  .summary-meta {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px 16px;
    width: 100%;
  }

  .summary-value {
    font-size: 13px;
  }

  /* K 线间隔/图表切换按钮在手机上变大，方便点击 */
  :deep(.n-button--tiny-type) {
    min-height: 32px;
    padding: 0 10px;
    font-size: 12px;
  }

  :deep(.n-button--small-type) {
    min-height: 36px;
  }
}

/* 手机端悬浮交易按钮 */
.mobile-trade-fab {
  position: fixed;
  right: 16px;
  /* 底部导航 h-12=48px + 16px 呼吸 + iOS 安全区 */
  bottom: calc(64px + env(safe-area-inset-bottom, 0px));
  z-index: 90;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 22px;
  border: 2px solid #000;
  background: #000;
  color: #fff;
  box-shadow: 4px 4px 0 rgba(0, 0, 0, 0.25);
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.06em;
  cursor: pointer;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
  animation: fab-in 0.2s ease-out;
}

.mobile-trade-fab:active {
  transform: translate(2px, 2px);
  box-shadow: 2px 2px 0 rgba(0, 0, 0, 0.25);
}

.mobile-trade-fab.fab-sell {
  background: var(--color-down);
  border-color: var(--color-down);
}

.fab-arrow {
  font-size: 16px;
  font-weight: 700;
  opacity: 0.85;
}

@keyframes fab-in {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}
</style>