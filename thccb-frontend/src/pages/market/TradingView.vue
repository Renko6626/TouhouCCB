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
const candleRefreshToken = ref(0)
let realtimeRefreshTimer: ReturnType<typeof setTimeout> | null = null

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

      <div class="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
      <!-- 左侧：主图与补充信息 -->
      <div>
        <NCard class="mb-6">
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
                <template v-if="activeChartType === 'candle'">
                  <span class="text-xs text-[#888] ml-2">周期:</span>
                  <NButton
                    v-for="opt in candleIntervalOptions"
                    :key="opt.value"
                    size="tiny"
                    :type="candleInterval === opt.value ? 'primary' : 'default'"
                    @click="candleInterval = opt.value"
                  >{{ opt.label }}</NButton>
                </template>
              </div>
            </div>
          </template>

          <div class="h-[300px] sm:h-[400px] md:h-[560px]">
            <PriceChart
              v-if="activeChartType === 'price' && selectedOutcomeId && marketStore.currentMarket"
              :outcome-id="selectedOutcomeId"
              height="100%"
            />
            <CandleChart
              v-else-if="selectedOutcomeId && marketStore.currentMarket"
              :outcome-id="selectedOutcomeId"
              :interval="candleInterval"
              :refresh-token="candleRefreshToken"
              :auto-refresh-ms="sse.isConnected.value ? 0 : 6000"
              height="100%"
            />
          </div>
        </NCard>

        <div class="grid grid-cols-1 gap-6 2xl:grid-cols-[minmax(0,0.95fr)_minmax(320px,0.75fr)]">
          <NCard>
            <template #header>
              <div class="flex items-center justify-between gap-4">
                <span class="font-bold text-black">市场选项</span>
                <MarketStatus :status="marketStore.currentMarket.status" />
              </div>
            </template>

            <div class="space-y-4">

              <div>
                <h4 class="font-semibold mb-3">市场选项</h4>
                <div class="space-y-3">
                  <OutcomeCard
                    v-for="outcome in marketStore.currentMarket.outcomes"
                    :key="outcome.id"
                    :outcome="outcome"
                    :is-selected="selectedOutcomeId === outcome.id"
                    @click="selectedOutcomeId = outcome.id"
                  />
                </div>
              </div>
            </div>
          </NCard>

          <div class="space-y-6">
            <NCard title="最近成交" v-if="marketStore.marketTrades.length">
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
      </div>

      <!-- 右侧：交易面板 -->
      <div class="space-y-4 xl:sticky xl:top-6 self-start">
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
      </div>
    </div>

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

</style>