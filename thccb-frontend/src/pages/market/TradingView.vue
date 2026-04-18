<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMarketStore } from '@/stores/market'
import { useUserStore } from '@/stores/user'
import { useAuthStore } from '@/stores/auth'
import { NButton, NCard, NSpin, NAlert, NTag, NEmpty, useMessage } from 'naive-ui'
import { useSSE } from '@/composables/useSSE'
import type { MarketEvent } from '@/types/api'
import TradePanel from '@/components/market/TradePanel.vue'
import MarketStatus from '@/components/market/MarketStatus.vue'
import OutcomeCard from '@/components/market/OutcomeCard.vue'
import PriceChart from '@/components/chart/PriceChart.vue'
import CandleChart from '@/components/chart/CandleChart.vue'

const route = useRoute()
const router = useRouter()
const marketStore = useMarketStore()
const userStore = useUserStore()
const authStore = useAuthStore()
const sse = useSSE()
const message = useMessage()

// 状态
const loading = ref(false)
const marketId = computed(() => parseInt(route.params.id as string))

// 交易表单
const tradeType = ref<'buy' | 'sell'>('buy')
const selectedOutcomeId = ref<number | null>(null)
const shares = ref(1)
import type { QuoteResponse } from '@/types/api'
const quoteResult = ref<QuoteResponse | null>(null)
const activeChartType = ref<'price' | 'candle'>('candle')
const candleInterval = ref<'10s' | '30s' | '1m' | '5m' | '15m' | '1h'>('1m')
const candleIntervalOptions = [
  { label: '10秒', value: '10s' },
  { label: '30秒', value: '30s' },
  { label: '1分钟', value: '1m' },
  { label: '5分钟', value: '5m' },
  { label: '15分钟', value: '15m' },
  { label: '1小时', value: '1h' },
] as const

// interval → lookback 联动：每个周期默认显示约 80 根 K 线
const LOOKBACK_MAP: Record<string, number> = {
  '10s': 15,       // 15 分钟 = 90 根
  '30s': 40,       // 40 分钟 = 80 根
  '1m': 80,        // 80 分钟 = 80 根
  '5m': 400,       // ~6.7 小时 = 80 根
  '15m': 1200,     // 20 小时 = 80 根
  '1h': 4800,      // ~3.3 天 = 80 根
}
const candleLookback = computed(() => LOOKBACK_MAP[candleInterval.value] || 80)

// 价格走势图时间范围
const priceLookback = ref(1440)  // 默认 24 小时
const priceLookbackOptions = [
  { label: '1小时', value: 60 },
  { label: '6小时', value: 360 },
  { label: '24小时', value: 1440 },
  { label: '3天', value: 4320 },
  { label: '7天', value: 10080 },
] as const

const candleRefreshToken = ref(0)
let realtimeRefreshTimer: ReturnType<typeof setTimeout> | null = null

// 手机端悬浮交易按钮：面板可见时自动隐藏
const tradePanelRef = ref<HTMLElement | null>(null)
const tradePanelVisible = ref(false)
let tradePanelObserver: IntersectionObserver | null = null

const scrollToTradePanel = () => {
  tradePanelRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

// 加载市场数据
const loadMarketData = async () => {
  if (!marketId.value) return
  
  loading.value = true
  try {
    await Promise.all([
      marketStore.fetchMarketDetail(marketId.value),
      marketStore.fetchMarketTrades(marketId.value)
    ])
    // 默认选择第一个选项
    if (marketStore.currentMarket?.outcomes?.length) {
      selectedOutcomeId.value = marketStore.currentMarket.outcomes[0]?.id ?? null
    }
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

// 初始化加载
onMounted(() => {
  loadMarketData()
  loadUserData()
  if (marketId.value) {
    sse.connect(marketId.value)
  }
  sse.on('snapshot', handleRealtimeEvent)
  sse.on('trade', handleRealtimeEvent)
  sse.on('market_status', handleRealtimeEvent)
})

// 交易面板可见性观察：挂载后按 ref 启用
watch(tradePanelRef, (el) => {
  if (tradePanelObserver) {
    tradePanelObserver.disconnect()
    tradePanelObserver = null
  }
  if (el && typeof IntersectionObserver !== 'undefined') {
    tradePanelObserver = new IntersectionObserver(
      ([entry]) => { tradePanelVisible.value = entry.isIntersecting },
      { threshold: 0.15 }
    )
    tradePanelObserver.observe(el)
  }
})

onBeforeUnmount(() => {
  sse.off('snapshot', handleRealtimeEvent)
  sse.off('trade', handleRealtimeEvent)
  sse.off('market_status', handleRealtimeEvent)
  sse.disconnect()
  if (realtimeRefreshTimer) {
    clearTimeout(realtimeRefreshTimer)
    realtimeRefreshTimer = null
  }
  if (quoteTimer) {
    clearTimeout(quoteTimer)
    quoteTimer = null
  }
  if (tradePanelObserver) {
    tradePanelObserver.disconnect()
    tradePanelObserver = null
  }
})

// 监听市场ID变化：先断开旧连接再连新的
watch(marketId, () => {
  sse.disconnect()
  loadMarketData()
  if (marketId.value) {
    sse.connect(marketId.value)
  }
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

// 统一 SSE 事件处理（snapshot/trade/market_status 逻辑相同）
const handleRealtimeEvent = (event: MarketEvent) => {
  if (event.market_id !== marketId.value) return
  // 交易执行期间暂停自动刷新，避免数据不一致
  if (marketStore.tradeLoading) return
  candleRefreshToken.value += 1
  scheduleRealtimeRefresh()
}

const realtimeStatusType = computed<'success' | 'warning'>(() => {
  return sse.isConnected.value ? 'success' : 'warning'
})

const realtimeStatusText = computed(() => {
  if (sse.isConnected.value) return '实时连接中'
  if (sse.reconnectCount.value > 0) return `重连中 (${sse.reconnectCount.value})`
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
  return userStore.getHoldingByOutcome(selectedOutcomeId.value)
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

// 获取交易报价
const getQuote = async () => {
  if (!selectedOutcomeId.value || shares.value <= 0) {
    quoteResult.value = null
    return
  }

  try {
    const result = await marketStore.getQuote(
      selectedOutcomeId.value,
      shares.value,
      tradeType.value
    )
    if (result?.data) {
      quoteResult.value = result.data
    }
  } catch (error) {
    quoteResult.value = null
  }
}

// 计算交易后现金
const estimatedNewCash = computed(() => {
  if (!quoteResult.value || !userStore.summary) return 0
  const netCost = quoteResult.value.net || 0
  return tradeType.value === 'buy' 
    ? userStore.summary.cash - netCost
    : userStore.summary.cash + Math.abs(netCost)
})

// 防抖获取报价，避免输入时连续发请求
let quoteTimer: ReturnType<typeof setTimeout> | null = null
const debouncedGetQuote = () => {
  if (quoteTimer) clearTimeout(quoteTimer)
  quoteTimer = setTimeout(() => getQuote(), 400)
}

// 监听交易参数变化
watch([tradeType, selectedOutcomeId, shares], () => {
  debouncedGetQuote()
}, { immediate: true })

// 执行交易（tradeLoading 由 marketStore 统一管理）
const executeTrade = async () => {
  if (!selectedOutcomeId.value || shares.value <= 0) return

  try {
    if (tradeType.value === 'buy') {
      await marketStore.buyShares(selectedOutcomeId.value, shares.value)
    } else {
      await marketStore.sellShares(selectedOutcomeId.value, shares.value)
    }

    message.success(`${tradeType.value === 'buy' ? '买入' : '卖出'}成功`)

    // 刷新数据
    await Promise.all([
      loadMarketData(),
      loadUserData()
    ])

    // 重置表单
    shares.value = 1
    quoteResult.value = null
  } catch (err: any) {
    message.error(err?.message || '交易失败，请重试')
  }
}

</script>

<template>
  <div class="trading-view-page">
    <!-- 加载状态 -->
    <div v-if="loading" class="text-center py-12">
      <NSpin size="large" />
      <p class="mt-4 text-black">加载市场数据中...</p>
    </div>

    <!-- 交易界面 -->
    <div v-else-if="marketStore.currentMarket" class="space-y-6">
      <!-- 市场概要栏 -->
      <div class="market-summary-bar">
        <div class="summary-main">
          <h2 class="summary-title">{{ marketStore.currentMarket.title }}</h2>
          <p class="summary-desc">{{ marketStore.currentMarket.description }}</p>
        </div>
        <div class="summary-meta">
          <div class="summary-item">
            <span class="summary-label">流动性</span>
            <span class="summary-value">¥{{ marketStore.currentMarket.liquidity_b.toLocaleString() }}</span>
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
            <span :class="['realtime-dot', sse.isConnected.value ? 'dot-on' : 'dot-off']">
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
                <template v-if="activeChartType === 'candle'">
                  <NButton
                    v-for="opt in candleIntervalOptions"
                    :key="opt.value"
                    size="tiny"
                    :type="candleInterval === opt.value ? 'primary' : 'default'"
                    @click="candleInterval = opt.value"
                  >{{ opt.label }}</NButton>
                </template>
                <template v-else>
                  <NButton
                    v-for="opt in priceLookbackOptions"
                    :key="opt.value"
                    size="tiny"
                    :type="priceLookback === opt.value ? 'primary' : 'default'"
                    @click="priceLookback = opt.value"
                  >{{ opt.label }}</NButton>
                </template>
              </div>
            </div>
          </template>

          <div class="h-[300px] sm:h-[400px] md:h-[560px]">
            <PriceChart
              v-if="activeChartType === 'price' && selectedOutcomeId && marketStore.currentMarket"
              :outcome-id="selectedOutcomeId"
              :lookback-minutes="priceLookback"
              height="100%"
            />
            <CandleChart
              v-else-if="selectedOutcomeId && marketStore.currentMarket"
              :outcome-id="selectedOutcomeId"
              :interval="candleInterval"
              :lookback-minutes="candleLookback"
              :refresh-token="candleRefreshToken"
              :auto-refresh-ms="sse.isConnected.value ? 0 : 6000"
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
            @update:selected-outcome-id="selectedOutcomeId = $event"
            @update:trade-type="tradeType = $event"
            @update:shares="shares = $event"
            @execute-trade="executeTrade"
          />
        </div>

        <!-- 4. 最近成交 (手机 row4 / 桌面 col1 row3) -->
        <NCard
          v-if="marketStore.marketTrades.length"
          title="最近成交"
          class="xl:col-start-1 xl:row-start-3"
        >
          <div class="max-h-[320px] space-y-2 overflow-auto pr-1">
            <div
              v-for="trade in marketStore.marketTrades.slice(0, 10)"
              :key="trade.id"
              class="flex justify-between items-center p-2"
              style="border-bottom: 1px solid #e0e0e0;"
            >
              <div>
                <span class="font-medium text-black">
                  {{ marketStore.currentMarket?.outcomes?.find((o: any) => o.id === trade.outcome_id)?.label || '未知选项' }}
                </span>
                <span class="text-sm ml-2" style="color:#666;">{{ trade.side === 'buy' ? '买入' : '卖出' }}</span>
              </div>
              <div class="text-right">
                <div class="font-medium text-black">{{ trade.shares }} 份</div>
                <div class="text-sm" style="color:#666;">¥{{ trade.gross.toFixed(2) }}</div>
              </div>
            </div>
          </div>
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

    <!-- 市场不存在 -->
    <div v-else class="text-center py-12">
      <NEmpty description="市场不存在或已被删除">
        <template #extra>
          <NButton type="primary" @click="router.push('/market/list')">
            返回市场列表
          </NButton>
        </template>
      </NEmpty>
    </div>
  </div>
</template>

<style scoped>
.trading-view-page {
  max-width: 1400px;
  margin: 0 auto;
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