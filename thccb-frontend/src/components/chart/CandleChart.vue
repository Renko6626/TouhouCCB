<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  LineSeries,
  createChart,
  type CandlestickData,
  type HistogramData,
  type LineData,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useChartData } from '@/composables/useChartData'
import type { Candle } from '@/types/api'

type ChartInterval = '10s' | '30s' | '1m' | '5m' | '15m' | '1h' | '1d'

const props = withDefaults(defineProps<{
  outcomeId: number
  interval?: ChartInterval
  width?: string
  height?: string
  autoRefreshMs?: number
  lookbackMinutes?: number
  refreshToken?: number
}>(), {
  interval: '1m',
  width: '100%',
  height: '400px',
  autoRefreshMs: 10000,
  lookbackMinutes: 60,
  refreshToken: 0,
})

const chartData = useChartData()
const chartRef = ref<HTMLDivElement | null>(null)
let chartInstance: IChartApi | null = null
let candleSeries: ISeriesApi<'Candlestick', Time> | null = null
let volumeSeries: ISeriesApi<'Histogram', Time> | null = null
let maSeries: ISeriesApi<'Line', Time> | null = null
const MA_PERIOD = 10
let refreshTimer: ReturnType<typeof setInterval> | null = null
let resizeObserver: ResizeObserver | null = null
const isRequesting = ref(false)

// 本地蜡烛缓存：key = UTC timestamp (秒)
const localCandles = ref<Map<number, Candle>>(new Map())

const hasData = computed(() => localCandles.value.size > 0)

const INTERVAL_SECONDS: Record<ChartInterval, number> = {
  '10s': 10, '30s': 30, '1m': 60, '5m': 300,
  '15m': 900, '1h': 3600, '1d': 86400,
}

const toUtcTimestamp = (iso: string): UTCTimestamp => {
  return Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp
}

const getLimitByWindow = () => {
  const step = INTERVAL_SECONDS[props.interval]
  return Math.max(50, Math.ceil((props.lookbackMinutes * 60) / step) + 8)
}

// 全量加载（初始加载或切换 outcome/interval 时）
const loadFull = async () => {
  if (!props.outcomeId || isRequesting.value) return

  const now = new Date()
  const lookbackMs = Math.max(5, props.lookbackMinutes) * 60 * 1000
  const fromTs = new Date(now.getTime() - lookbackMs).toISOString()
  const toTs = now.toISOString()

  isRequesting.value = true
  try {
    const resp = await chartData.getCandles(
      props.outcomeId, props.interval, fromTs, toTs,
      true, getLimitByWindow(), 50000, false,
    )
    if (resp) {
      // 重建本地缓存
      const map = new Map<number, Candle>()
      for (const c of resp.candles) {
        map.set(toUtcTimestamp(c.t), c)
      }
      localCandles.value = map
      renderFull()
    }
  } finally {
    isRequesting.value = false
  }
}

// 增量加载（轮询时）：只请求最后 2 个 bucket 的数据
const loadIncremental = async () => {
  if (!props.outcomeId || isRequesting.value) return

  const step = INTERVAL_SECONDS[props.interval]
  const now = new Date()
  // 向前取 2 个 bucket，确保能覆盖当前正在形成的蜡烛和刚闭合的蜡烛
  const fromTs = new Date(now.getTime() - step * 2 * 1000).toISOString()
  const toTs = now.toISOString()

  isRequesting.value = true
  try {
    const resp = await chartData.getCandles(
      props.outcomeId, props.interval, fromTs, toTs,
      false, 10, 50000, true,
    )
    if (resp && resp.candles.length > 0) {
      applyIncremental(resp.candles)
    }
  } finally {
    isRequesting.value = false
  }
}

// 将增量蜡烛合并到本地缓存 + 用 update() 推送给 lightweight-charts
const applyIncremental = (newCandles: Candle[]) => {
  if (!candleSeries || !volumeSeries || !maSeries) return

  for (const c of newCandles) {
    const ts = toUtcTimestamp(c.t)
    localCandles.value.set(ts, c)

    candleSeries.update({
      time: ts,
      open: c.o,
      high: c.h,
      low: c.l,
      close: c.c,
    })
    volumeSeries.update({
      time: ts,
      value: c.v,
      color: c.c >= c.o ? '#16a34a80' : '#dc262680',
    })
  }

  // 增量更新后重算均线（用最新的本地缓存）
  const sorted = [...localCandles.value.entries()].sort(([a], [b]) => a - b)
  maSeries.setData(calculateMA(sorted))
}

// 计算移动平均线
const calculateMA = (sorted: [number, import('@/types/api').Candle][]): LineData<UTCTimestamp>[] => {
  const result: LineData<UTCTimestamp>[] = []
  for (let i = MA_PERIOD - 1; i < sorted.length; i++) {
    let sum = 0
    for (let j = i - MA_PERIOD + 1; j <= i; j++) {
      sum += sorted[j][1].c
    }
    result.push({
      time: sorted[i][0] as UTCTimestamp,
      value: sum / MA_PERIOD,
    })
  }
  return result
}

// 全量渲染（初始加载 / 切换时）
const renderFull = () => {
  if (!candleSeries || !volumeSeries || !maSeries) return

  const sorted = [...localCandles.value.entries()]
    .sort(([a], [b]) => a - b)

  const candleSeriesData: CandlestickData<UTCTimestamp>[] = sorted.map(([ts, c]) => ({
    time: ts as UTCTimestamp,
    open: c.o, high: c.h, low: c.l, close: c.c,
  }))

  const volumeSeriesData: HistogramData<UTCTimestamp>[] = sorted.map(([ts, c]) => ({
    time: ts as UTCTimestamp,
    value: c.v,
    color: c.c >= c.o ? '#16a34a80' : '#dc262680',
  }))

  candleSeries.setData(candleSeriesData)
  volumeSeries.setData(volumeSeriesData)
  maSeries.setData(calculateMA(sorted))
  chartInstance?.timeScale().fitContent()
}

const initChart = () => {
  if (!chartRef.value) return

  chartInstance = createChart(chartRef.value, {
    layout: {
      background: { type: ColorType.Solid, color: '#ffffff' },
      textColor: '#333',
    },
    grid: {
      vertLines: { color: '#e0e0e0', style: 1 },
      horzLines: { color: '#e0e0e0', style: 1 },
    },
    rightPriceScale: {
      borderColor: '#000',
      scaleMargins: { top: 0.15, bottom: 0.25 },
    },
    timeScale: {
      borderColor: '#000',
      timeVisible: true,
      secondsVisible: props.interval === '10s' || props.interval === '30s',
    },
    crosshair: { mode: 1 },
    width: chartRef.value.clientWidth,
    height: chartRef.value.clientHeight,
  })

  candleSeries = chartInstance.addSeries(CandlestickSeries, {
    upColor: '#16a34a',
    downColor: '#dc2626',
    wickUpColor: '#16a34a',
    wickDownColor: '#dc2626',
    borderVisible: false,
  })

  maSeries = chartInstance.addSeries(LineSeries, {
    color: '#f59e0b',
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  })

  volumeSeries = chartInstance.addSeries(HistogramSeries, {
    color: '#94a3b8',
    priceFormat: { type: 'volume' },
    priceScaleId: '',
  })

  volumeSeries.priceScale().applyOptions({
    scaleMargins: { top: 0.8, bottom: 0 },
  })
}

const setupResizeObserver = () => {
  if (!chartRef.value || !chartInstance) return
  resizeObserver = new ResizeObserver((entries) => {
    const entry = entries[0]
    if (!entry) return
    chartInstance?.applyOptions({
      width: entry.contentRect.width,
      height: entry.contentRect.height,
    })
  })
  resizeObserver.observe(chartRef.value)
}

const resetRefreshTimer = () => {
  if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
  if (!props.autoRefreshMs) return
  const ms = Math.max(3000, props.autoRefreshMs)
  refreshTimer = setInterval(() => loadIncremental(), ms)
}

onMounted(() => {
  nextTick(() => {
    initChart()
    setupResizeObserver()
    loadFull()
    resetRefreshTimer()
  })
})

// 切换 outcome 或 interval 时全量重载
watch(() => [props.outcomeId, props.interval], () => {
  localCandles.value.clear()
  chartInstance?.applyOptions({
    timeScale: {
      secondsVisible: props.interval === '10s' || props.interval === '30s',
    },
  })
  loadFull()
  resetRefreshTimer()
})

watch(() => props.lookbackMinutes, () => { loadFull() })
watch(() => props.autoRefreshMs, () => { resetRefreshTimer() })
watch(() => props.refreshToken, () => { loadIncremental() })

onUnmounted(() => {
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null }
  if (chartInstance) { chartInstance.remove(); chartInstance = null }
  if (resizeObserver) { resizeObserver.disconnect(); resizeObserver = null }
})
</script>

<template>
  <div
    class="candle-chart-container"
    :style="{ width: props.width, height: props.height }"
  >
    <div ref="chartRef" style="width:100%;height:100%"></div>

    <div v-if="chartData.loading && !hasData" class="overlay-state">
      <p class="overlay-text">加载K线数据中...</p>
    </div>

    <div v-else-if="chartData.error && !hasData" class="overlay-state">
      <p class="overlay-text overlay-text--error">{{ chartData.error }}</p>
      <button @click="loadFull()" class="overlay-btn">重试</button>
    </div>

    <div v-else-if="!chartData.loading && !chartData.error && !hasData" class="overlay-state">
      <p class="overlay-text">暂无K线数据</p>
    </div>
  </div>
</template>

<style scoped>
.candle-chart-container {
  position: relative;
}

.overlay-state {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: rgba(255, 255, 255, 0.88);
}

.overlay-text {
  font-size: 14px;
  color: #555;
}

.overlay-text--error {
  color: #000;
  font-weight: 600;
}

.overlay-btn {
  margin-top: 12px;
  padding: 4px 16px;
  background: #000;
  color: #fff;
  border: 2px solid #000;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}

.overlay-btn:hover {
  transform: translate(-1px, -1px);
  box-shadow: 3px 3px 0 #000;
}
</style>
