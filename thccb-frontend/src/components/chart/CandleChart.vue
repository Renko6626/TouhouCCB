<script setup lang="ts">
import { computed, inject, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
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
import { chartApi } from '@/api/chart'
import type { Candle } from '@/types/api'
import { getPalette, withAlpha } from '@/utils/palette'
import { MarketRealtimeKey } from '@/composables/useMarketRealtime'

type ChartInterval = '10s' | '30s' | '1m' | '5m' | '15m' | '1h' | '1d'

const props = withDefaults(defineProps<{
  outcomeId: number
  interval?: ChartInterval
  width?: string
  height?: string
  lookbackMinutes?: number
}>(), {
  interval: '1m',
  width: '100%',
  height: '400px',
  lookbackMinutes: 60,
})

const realtime = inject(MarketRealtimeKey, null)

const chartRef = ref<HTMLDivElement | null>(null)
let chartInstance: IChartApi | null = null
let candleSeries: ISeriesApi<'Candlestick', Time> | null = null
let volumeSeries: ISeriesApi<'Histogram', Time> | null = null
let maSeries: ISeriesApi<'Line', Time> | null = null
const MA_PERIOD = 10
let resizeObserver: ResizeObserver | null = null
let resizeRafId: number | null = null

const loading = ref(false)
const error = ref<string | null>(null)
const candleCount = ref(0)
const hasData = computed(() => candleCount.value > 0)

const INTERVAL_SECONDS: Record<ChartInterval, number> = {
  '10s': 10, '30s': 30, '1m': 60, '5m': 300,
  '15m': 900, '1h': 3600, '1d': 86400,
}

// ── 本地增量状态（incremental update 核心） ─────────────
// 已闭合 candles 的 close 数组（与 publishedTimes 同序），用于 O(MA_PERIOD) 增量算 MA
let publishedCloses: number[] = []
// 当前正在形成的 candle（forming）；trade 来时原地更新 h/l/c/v
let currentCandle: { t: number; o: number; h: number; l: number; c: number; v: number } | null = null

const stepSeconds = computed(() => INTERVAL_SECONDS[props.interval])

const toUtcTimestamp = (iso: string): UTCTimestamp =>
  Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp

const getLimitByWindow = () => {
  const step = stepSeconds.value
  return Math.max(50, Math.ceil((props.lookbackMinutes * 60) / step) + 8)
}

// ── 增量 MA 计算：仅用最近 MA_PERIOD-1 个已闭合 close + 当前 forming close
const computeFormingMa = (): number | null => {
  if (!currentCandle) return null
  const total = publishedCloses.length + 1
  if (total < MA_PERIOD) return null
  let sum = currentCandle.c
  for (let i = 0; i < MA_PERIOD - 1; i++) {
    sum += publishedCloses[publishedCloses.length - 1 - i]!
  }
  return sum / MA_PERIOD
}

const pushFormingToChart = () => {
  if (!currentCandle || !candleSeries || !volumeSeries) return
  const t = currentCandle.t as UTCTimestamp
  candleSeries.update({
    time: t,
    open: currentCandle.o,
    high: currentCandle.h,
    low: currentCandle.l,
    close: currentCandle.c,
  })
  const p = getPalette()
  volumeSeries.update({
    time: t,
    value: currentCandle.v,
    color: withAlpha(currentCandle.c >= currentCandle.o ? p.up : p.down, 0x80),
  })
  if (maSeries) {
    const ma = computeFormingMa()
    if (ma !== null) maSeries.update({ time: t, value: ma })
  }
}

// 全量加载（初始 / 切 outcome/interval/lookback / gap reconcile）
const loadFull = async () => {
  if (!props.outcomeId) return
  loading.value = true
  error.value = null
  try {
    const now = new Date()
    const lookbackMs = Math.max(5, props.lookbackMinutes) * 60 * 1000
    const fromTs = new Date(now.getTime() - lookbackMs).toISOString()
    const toTs = now.toISOString()
    const resp = await chartApi.getCandles(
      props.outcomeId, props.interval, fromTs, toTs,
      true, getLimitByWindow(), 50000,
    )
    if (!resp || !resp.candles) {
      candleCount.value = 0
      return
    }
    const candles = [...resp.candles].sort((a, b) =>
      toUtcTimestamp(a.t) - toUtcTimestamp(b.t),
    )
    candleCount.value = candles.length

    await nextTick()
    if (!chartInstance) initChart()
    if (!candleSeries || !volumeSeries || !maSeries) return

    renderFull(candles)
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'K线数据加载失败'
    console.error('[CandleChart] loadFull failed:', err)
  } finally {
    loading.value = false
  }
}

// 一次性 setData 所有 candles 并初始化本地增量状态
const renderFull = (candles: Candle[]) => {
  if (!candleSeries || !volumeSeries || !maSeries) return

  const candleData: CandlestickData<UTCTimestamp>[] = []
  const volumeData: HistogramData<UTCTimestamp>[] = []
  const p = getPalette()

  for (const c of candles) {
    const t = toUtcTimestamp(c.t)
    candleData.push({ time: t, open: c.o, high: c.h, low: c.l, close: c.c })
    volumeData.push({
      time: t,
      value: c.v,
      color: withAlpha(c.c >= c.o ? p.up : p.down, 0x80),
    })
  }

  candleSeries.setData(candleData)
  volumeSeries.setData(volumeData)

  // 计算所有 MA 点（仅在初次渲染 / reconcile 时做一次 O(N×MA_PERIOD)）
  const maData: LineData<UTCTimestamp>[] = []
  for (let i = MA_PERIOD - 1; i < candles.length; i++) {
    let sum = 0
    for (let j = i - MA_PERIOD + 1; j <= i; j++) sum += candles[j]!.c
    maData.push({ time: toUtcTimestamp(candles[i]!.t), value: sum / MA_PERIOD })
  }
  maSeries.setData(maData)

  // 初始化本地增量状态：永远把最后一根 candle 当 forming。
  // backend `fill=true` 模式保证返回的最后一根 = "当前 bucket"（用空 candle 填到 now），
  // 所以不用本地 Date.now() 做判断（用户系统时钟偏差会错位归类）。
  if (candles.length === 0) {
    publishedCloses = []
    currentCandle = null
  } else {
    publishedCloses = candles.slice(0, -1).map(c => c.c)
    const last = candles[candles.length - 1]!
    currentCandle = {
      t: toUtcTimestamp(last.t) as number,
      o: last.o, h: last.h, l: last.l, c: last.c, v: last.v,
    }
  }

  chartInstance?.timeScale().fitContent()
}

// SSE trade 来时，把它合并进本地 currentCandle 并 push 给 lightweight-charts
const applyTrade = (price: number, shares: number, tsMs: number) => {
  if (!candleSeries) return
  const tsSec = Math.floor(tsMs / 1000)
  const bucketStart = Math.floor(tsSec / stepSeconds.value) * stepSeconds.value

  if (!currentCandle) {
    // 第一根：用 price 当 open
    currentCandle = { t: bucketStart, o: price, h: price, l: price, c: price, v: shares }
    pushFormingToChart()
    candleCount.value += 1
    return
  }

  if (bucketStart === currentCandle.t) {
    // 同 bucket：原地更新
    currentCandle.h = Math.max(currentCandle.h, price)
    currentCandle.l = Math.min(currentCandle.l, price)
    currentCandle.c = price
    currentCandle.v += shares
    pushFormingToChart()
    return
  }

  if (bucketStart === currentCandle.t + stepSeconds.value) {
    // 正常切到下一个 bucket：闭合 currentCandle，开新 candle
    publishedCloses.push(currentCandle.c)
    const prevClose = currentCandle.c
    currentCandle = {
      t: bucketStart,
      o: prevClose,
      h: Math.max(prevClose, price),
      l: Math.min(prevClose, price),
      c: price,
      v: shares,
    }
    pushFormingToChart()
    candleCount.value += 1
    return
  }

  if (bucketStart > currentCandle.t + stepSeconds.value) {
    // 跨过 ≥1 个完整 bucket（用户挂后台 / 间隔时段无 trade / SSE 期间漏消息）
    // → 中间 bucket 数据本地无法合成，触发 loadFull 让 backend fill=true 补齐
    console.warn('[CandleChart] multi-bucket gap, triggering reload')
    loadFull()
    return
  }

  // bucketStart < currentCandle.t：时钟漂移 / 乱序事件 → 忽略（极罕见）
  console.warn('[CandleChart] out-of-order trade ts ignored:', tsMs)
}

const initChart = () => {
  if (!chartRef.value || chartInstance) return

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

  const palette = getPalette()
  candleSeries = chartInstance.addSeries(CandlestickSeries, {
    upColor: palette.up,
    downColor: palette.down,
    wickUpColor: palette.up,
    wickDownColor: palette.down,
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
    const { width, height } = entry.contentRect
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
  setupResizeObserver()
})

// 切 outcome / interval / lookback → 全量重载
watch(() => [props.outcomeId, props.interval, props.lookbackMinutes], () => {
  chartInstance?.applyOptions({
    timeScale: {
      secondsVisible: props.interval === '10s' || props.interval === '30s',
    },
  })
  loadFull()
})

// SSE 实时增量
if (realtime) {
  watch(realtime.latestTrade, (trade) => {
    if (!trade) return
    const price = realtime.pricesByOutcome.value.get(props.outcomeId)
    if (price === undefined) return
    const tsMs = new Date(trade.timestamp).getTime()
    // 只有交易的 outcome 才把 shares 计入 volume；其他 outcome 的图表 v 只随价格动
    const sharesForThisChart = trade.outcome_id === props.outcomeId ? trade.shares : 0
    applyTrade(price, sharesForThisChart, tsMs)
  })

  watch(realtime.gapToken, () => {
    if (realtime.gapToken.value > 0) loadFull()
  })
}

onUnmounted(() => {
  if (resizeRafId !== null) { cancelAnimationFrame(resizeRafId); resizeRafId = null }
  if (resizeObserver) { resizeObserver.disconnect(); resizeObserver = null }
  if (chartInstance) { chartInstance.remove(); chartInstance = null }
})
</script>

<template>
  <div
    class="candle-chart-container"
    :style="{ width: props.width, height: props.height }"
  >
    <div ref="chartRef" style="width:100%;height:100%"></div>

    <div v-if="loading && !hasData" class="overlay-state">
      <p class="overlay-text">加载K线数据中...</p>
    </div>

    <div v-else-if="error && !hasData" class="overlay-state">
      <p class="overlay-text overlay-text--error">{{ error }}</p>
      <button @click="loadFull()" class="overlay-btn">重试</button>
    </div>

    <div v-else-if="!loading && !error && !hasData" class="overlay-state">
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
