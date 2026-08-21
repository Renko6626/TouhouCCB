<script setup lang="ts">
import { ref, onMounted, watch, onUnmounted, nextTick, computed, inject } from 'vue'
import {
  createChart,
  ColorType,
  AreaSeries,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { chartApi } from '@/api/chart'
import type { ChartInterval } from '@/types/api'
import { getPalette } from '@/utils/palette'
import { MarketRealtimeKey } from '@/composables/useMarketRealtime'

const props = withDefaults(defineProps<{
  outcomeId: number
  interval?: ChartInterval
  width?: string
  height?: string
}>(), {
  interval: '1m',
  width: '100%',
  height: '400px',
})

// 跟 CandleChart 同款 LOOKBACK_MAP；高频档拉更多历史点
const LOOKBACK_MINUTES_MAP: Record<ChartInterval, number> = {
  '10s': 500,
  '1m':  480,
  '15m': 1200,
  '1h':  4800,
}

const INTERVAL_SECONDS: Record<ChartInterval, number> = {
  '10s': 10, '1m': 60, '15m': 900, '1h': 3600,
}

const getLimitByWindow = () => {
  const step = INTERVAL_SECONDS[props.interval]
  return Math.max(50, Math.ceil((LOOKBACK_MINUTES_MAP[props.interval] * 60) / step) + 8)
}

// inject realtime —— 必须在 useMarketRealtime provider 下使用（TradingView 提供）
const realtime = inject(MarketRealtimeKey, null)

const chartRef = ref<HTMLDivElement | null>(null)
let chartInstance: IChartApi | null = null
let areaSeries: ISeriesApi<'Area', Time> | null = null
let resizeObserver: ResizeObserver | null = null
let resizeRafId: number | null = null

// 本地状态
const loading = ref(false)
const error = ref<string | null>(null)
const pointCount = ref(0)
const firstPrice = ref<number | null>(null)
const lastPrice = ref<number | null>(null)
// 上一次写入图表的 timestamp（秒）—— 防止两笔同 ts 交易触发 lightweight-charts
// 的"time < last"错误；同 ts 的后一笔走 replace（update 行为同 ts 即 replace）
let lastWrittenTs: number = 0

// 1Hz ticker：推进合成"现在"端点 + 平移可视窗，让时间轴跟着 wall clock 走，
// 即使没新交易曲线也在向右生长（实时感）。纯前端定时器，零服务端负载。
let tickerId: ReturnType<typeof setInterval> | null = null
// 用户是否拖动时间轴查看历史 —— 拖到 to < now-2s 视为"在看历史"，
// ticker 暂停平移；拖回贴右端（to ≈ now）自动恢复。
// 监测靠 subscribeVisibleTimeRangeChange 回调：包括我们自己的 setVisibleRange
// 也会触发，但用 (nowSec - to) 阈值判断不需要区分来源。
let userScrolledBack = false

const hasData = computed(() => pointCount.value > 0)

const priceDirection = computed<'up' | 'down' | 'neutral'>(() => {
  if (firstPrice.value === null || lastPrice.value === null) return 'neutral'
  if (lastPrice.value === firstPrice.value) return 'neutral'
  return lastPrice.value > firstPrice.value ? 'up' : 'down'
})

const hexToRgba = (hex: string, alpha: number): string => {
  const h = hex.replace('#', '')
  const r = parseInt(h.slice(0, 2), 16)
  const g = parseInt(h.slice(2, 4), 16)
  const b = parseInt(h.slice(4, 6), 16)
  return `rgba(${r},${g},${b},${alpha})`
}

const buildColors = () => {
  const p = getPalette()
  return {
    up:   { line: p.up,   topFill: hexToRgba(p.up,   0.25), bottomFill: hexToRgba(p.up,   0.02) },
    down: { line: p.down, topFill: hexToRgba(p.down, 0.25), bottomFill: hexToRgba(p.down, 0.02) },
    neutral: { line: p.neutral, topFill: 'rgba(0,0,0,0.12)', bottomFill: 'rgba(0,0,0,0.02)' },
  }
}

const applyDirectionColors = () => {
  if (!areaSeries) return
  const c = buildColors()[priceDirection.value]
  areaSeries.applyOptions({
    lineColor: c.line,
    topColor: c.topFill,
    bottomColor: c.bottomFill,
    crosshairMarkerBorderColor: c.line,
  })
}

const initChart = () => {
  if (!chartRef.value || chartInstance) return

  chartInstance = createChart(chartRef.value, {
    layout: {
      background: { type: ColorType.Solid, color: '#ffffff' },
      textColor: '#333',
    },
    grid: {
      vertLines: { color: '#f0f0f0' },
      horzLines: { color: '#f0f0f0' },
    },
    rightPriceScale: {
      borderColor: '#e0e0e0',
      scaleMargins: { top: 0.1, bottom: 0.05 },
    },
    timeScale: {
      borderColor: '#e0e0e0',
      timeVisible: true,
      secondsVisible: props.interval === '10s',
    },
    crosshair: { mode: 0 },
    width: chartRef.value.clientWidth,
    height: chartRef.value.clientHeight,
  })

  const c = buildColors().neutral
  areaSeries = chartInstance.addSeries(AreaSeries, {
    lineColor: c.line,
    lineWidth: 2,
    topColor: c.topFill,
    bottomColor: c.bottomFill,
    crosshairMarkerRadius: 4,
    crosshairMarkerBorderWidth: 2,
    crosshairMarkerBorderColor: c.line,
    crosshairMarkerBackgroundColor: '#fff',
    priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
  })

  // 监听可视窗变化（包括用户拖动 + 我们自己 setVisibleRange）：
  // 用 nowSec - to > 2 判断用户是否拖到了"看历史"位置；不需要区分事件来源。
  chartInstance.timeScale().subscribeVisibleTimeRangeChange((range) => {
    if (!range) return
    const nowSec = Math.floor(Date.now() / 1000)
    userScrolledBack = (nowSec - (range.to as number)) > 2
  })
}

// 全量加载（初始 / 切换 outcome+interval / gap reconcile / 重连）
const loadFull = async () => {
  if (!props.outcomeId) return
  loading.value = true
  error.value = null
  try {
    const lookbackMin = LOOKBACK_MINUTES_MAP[props.interval]
    const now = new Date()
    const fromTs = new Date(now.getTime() - lookbackMin * 60 * 1000).toISOString()
    const toTs = now.toISOString()
    const resp = await chartApi.getCandles(
      props.outcomeId, props.interval, fromTs, toTs,
      true,  // fill=true，曲线在无 trade 期间用 prev_close 平直延伸
      getLimitByWindow(),
    )
    if (!resp || !resp.candles) {
      pointCount.value = 0
      return
    }

    const candles = resp.candles
    pointCount.value = candles.length
    firstPrice.value = candles[0]?.c ?? null
    lastPrice.value = candles[candles.length - 1]?.c ?? null

    await nextTick()
    if (!chartInstance) initChart()
    if (!areaSeries) return

    const fromTsSec = Math.floor(new Date(fromTs).getTime() / 1000) as UTCTimestamp
    const toTsSec = Math.floor(now.getTime() / 1000) as UTCTimestamp

    // 用每根 candle 的 close 价作为折线点
    const data = candles.map(c => ({
      time: Math.floor(new Date(c.t).getTime() / 1000) as UTCTimestamp,
      value: c.c,
    }))
    // 追加"现在"合成端点
    if (data.length > 0) {
      const last = data[data.length - 1]!
      if ((last.time as number) < (toTsSec as number)) {
        data.push({ time: toTsSec, value: last.value })
      }
    }
    areaSeries.setData(data)
    lastWrittenTs = data.length > 0 ? (data[data.length - 1]!.time as number) : 0
    applyDirectionColors()

    if (data.length > 0) {
      chartInstance?.timeScale().setVisibleRange({ from: fromTsSec, to: toTsSec })
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '价格数据加载失败'
    console.error('[PriceChart] loadFull failed:', err)
    pointCount.value = 0
    firstPrice.value = null
    lastPrice.value = null
    lastWrittenTs = 0
    if (areaSeries) areaSeries.setData([])
  } finally {
    loading.value = false
  }
}

// 增量 append 一个点（SSE trade 驱动）
const appendPoint = (price: number, tsMs: number) => {
  if (!areaSeries) return
  // 秒级 ts；lightweight-charts 要求 time 严格递增，等 ts 算同点 replace
  let tsSec = Math.floor(tsMs / 1000) as UTCTimestamp
  if ((tsSec as number) < lastWrittenTs) {
    // 时钟漂移或乱序事件 → 强制 +1s 单调（极罕见）
    tsSec = (lastWrittenTs + 1) as UTCTimestamp
  }
  areaSeries.update({ time: tsSec, value: price })
  lastWrittenTs = tsSec as number
  pointCount.value += 1
  if (firstPrice.value === null) firstPrice.value = price
  lastPrice.value = price
  applyDirectionColors()
}

// 1Hz ticker：推进合成"现在"端点（沿用最后一笔价格）+ 平移可视窗。
// 不调 backend、不发请求；CPU 单浏览器 < 0.1%。
// 与 appendPoint 共享 lastWrittenTs：同秒成交会 replace ticker 写入的占位。
// 用户拖动看历史时（userScrolledBack=true）暂停平移，避免被 1Hz 强制拉回右端。
const startTicker = () => {
  if (tickerId !== null) return
  tickerId = setInterval(() => {
    if (!areaSeries || !chartInstance) return
    if (lastPrice.value === null) return
    const nowSec = Math.floor(Date.now() / 1000)
    // 继续推进合成端点（即使用户在看历史，数据还是要更新到现在；只是不平移视窗）
    if (nowSec > lastWrittenTs) {
      areaSeries.update({ time: nowSec as UTCTimestamp, value: lastPrice.value })
      lastWrittenTs = nowSec
    }
    if (userScrolledBack) return  // 用户在看历史，不抢他的视野
    const lookbackSec = LOOKBACK_MINUTES_MAP[props.interval] * 60
    chartInstance.timeScale().setVisibleRange({
      from: (nowSec - lookbackSec) as UTCTimestamp,
      to: nowSec as UTCTimestamp,
    })
  }, 1000)
}

const stopTicker = () => {
  if (tickerId !== null) {
    clearInterval(tickerId)
    tickerId = null
  }
}

// resize
const setupResize = () => {
  if (!chartRef.value || !chartInstance) return
  resizeObserver = new ResizeObserver((entries) => {
    const e = entries[0]
    if (!e) return
    const { width, height } = e.contentRect
    if (resizeRafId !== null) cancelAnimationFrame(resizeRafId)
    resizeRafId = requestAnimationFrame(() => {
      resizeRafId = null
      chartInstance?.applyOptions({ width, height })
    })
  })
  resizeObserver.observe(chartRef.value)
}

onMounted(async () => {
  await loadFull()
  setupResize()
  startTicker()
})

// 切换 outcome / interval → 完全重载
watch(() => [props.outcomeId, props.interval], () => {
  chartInstance?.applyOptions({
    timeScale: { secondsVisible: props.interval === '10s' },
  })
  loadFull()
})

// SSE 实时增量：监听 latestTrade
if (realtime) {
  watch(realtime.latestTrade, (trade) => {
    if (!trade) return
    // 用 pricesByOutcome 取当前 outcome 的最新价（含其他 outcome 联动 patch 后的值）
    const price = realtime.pricesByOutcome.value.get(props.outcomeId)
    if (price === undefined) return
    const tsMs = new Date(trade.timestamp).getTime()
    appendPoint(price, tsMs)
  })

  // tick 帧：一帧多笔逐笔 append（用每笔的 market_prices_post 取本 outcome 的成交后价，
  // 保证帧内多笔的中间点不丢；latestTrade watcher 在 tick 模式下自然沉默，见 TradingView 注释）
  watch(realtime.latestTick, (frame) => {
    if (!frame) return
    const order = realtime.outcomesOrder.value
    const idx = order.indexOf(props.outcomeId)
    for (const t of frame.trades) {
      const price = idx >= 0 && t.market_prices_post?.length === order.length
        ? t.market_prices_post[idx]!
        : (t.outcome_id === props.outcomeId ? t.post_market_price : undefined)
      if (price === undefined) continue
      appendPoint(price, new Date(t.timestamp).getTime())
    }
  })

  // gap 检测：seq 不连续 → silent reload 当前可见区段
  watch(realtime.gapToken, () => {
    if (realtime.gapToken.value > 0) loadFull()
  })
}

onUnmounted(() => {
  stopTicker()
  if (resizeRafId !== null) { cancelAnimationFrame(resizeRafId); resizeRafId = null }
  if (resizeObserver) { resizeObserver.disconnect(); resizeObserver = null }
  if (chartInstance) { chartInstance.remove(); chartInstance = null }
})
</script>

<template>
  <div
    :style="{ width: props.width || '100%', height: props.height || '400px' }"
    class="price-chart-container"
  >
    <div v-if="loading && !hasData" class="chart-state">
      <p class="chart-state-text">加载中...</p>
    </div>

    <div v-else-if="error && !hasData" class="chart-state">
      <p class="chart-state-text chart-state-text--error">{{ error }}</p>
      <button @click="loadFull()" class="chart-retry-btn">重试</button>
    </div>

    <div v-else-if="!loading && !error && !hasData" class="chart-state">
      <p class="chart-state-text">暂无价格数据</p>
    </div>

    <div v-show="hasData" ref="chartRef" style="width:100%;height:100%"></div>
  </div>
</template>

<style scoped>
.price-chart-container {
  position: relative;
}

.chart-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  min-height: 200px;
}

.chart-state-text {
  font-size: 13px;
  color: #888;
}

.chart-state-text--error {
  color: #000;
  font-weight: 600;
}

.chart-retry-btn {
  margin-top: 10px;
  padding: 4px 14px;
  background: #000;
  color: #fff;
  border: 2px solid #000;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}
</style>
