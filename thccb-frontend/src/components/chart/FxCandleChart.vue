<script setup lang="ts">
// FX 专用 K 线组件：复用市场页 CandleChart 的成熟行为（forming candle、空桶填充、
// 增量 MA、drag 暂停 ticker、resize、gap reload），但数据源换成 `fxApi.getChart`
// + 页面转发进来的 `fxApi` SSE tick。
//
// 价格精度：右轴 `localization.priceFormatter` 与 tooltip 共用 `formatFxPrice`
// （按数量级动态小数位）；成交量是「金额」口径，用 `formatFxAmount`，两者分开。
// lightweight-charts 只接受 number，所以在 `toCandleData` 适配层一次性转换，
// 展示时再由同一 formatter 还原字符串。
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  LineSeries,
  createChart,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type LineData,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { fxApi, formatFxAmount, formatFxPrice, fxPricePrecision } from '@/api/fx'
import type { FxChartInterval, FxPriceTick } from '@/types/fx'
import {
  FxCandleEngine,
  FX_MA_PERIODS,
  type FxCandle,
} from '@/utils/fxCandle'
import { getPalette, withAlpha } from '@/utils/palette'

const props = withDefaults(
  defineProps<{
    pairId: number
    interval?: FxChartInterval
    /** 页面从 FxStream 转发的实时价格 tick（价格保持字符串语义） */
    tick?: FxPriceTick | null
    /** 父页面递增该值可强制整段重载（成交后刷新成交量） */
    reloadToken?: number
    height?: string
  }>(),
  {
    interval: '1m',
    tick: null,
    reloadToken: 0,
    height: '100%',
  },
)

const LOOKBACK_MINUTES: Record<FxChartInterval, number> = { '1m': 480, '15m': 1200, '1h': 4800 }
const INTERVAL_SECONDS: Record<FxChartInterval, number> = { '1m': 60, '15m': 900, '1h': 3600 }
const DEFAULT_VISIBLE_CANDLE_COUNT = 80
const MA_COLORS: Record<number, string> = { 10: '#f59e0b', 20: '#2563eb' }

interface FxLegend {
  t: number | null
  o: number | null
  h: number | null
  l: number | null
  c: number | null
  v: number
  changePct: number | null
  ma: Record<number, number | null>
}

const chartRef = ref<HTMLDivElement | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const candleCount = ref(0)
const legend = ref<FxLegend | null>(null)

let chartInstance: IChartApi | null = null
let candleSeries: ISeriesApi<'Candlestick', Time> | null = null
let volumeSeries: ISeriesApi<'Histogram', Time> | null = null
const maSeries: Record<number, ISeriesApi<'Line', Time> | null> = {}

let resizeObserver: ResizeObserver | null = null
let resizeRafId: number | null = null
let tickerId: ReturnType<typeof setInterval> | null = null
let userScrolledBack = false
let lastPricePrecision = 0
let loadGen = 0

// 周期切换时整体重建（步长变化会改变 bucket 归类）
let engine = new FxCandleEngine(INTERVAL_SECONDS[props.interval], FX_MA_PERIODS)

const toCandleData = (c: FxCandle): CandlestickData<UTCTimestamp> => ({
  time: c.t as UTCTimestamp,
  open: c.o,
  high: c.h,
  low: c.l,
  close: c.c,
})

const toVolumeData = (c: FxCandle): HistogramData<UTCTimestamp> => {
  const palette = getPalette()
  return {
    time: c.t as UTCTimestamp,
    value: c.v,
    color: withAlpha(c.c >= c.o ? palette.up : palette.down, 0x80),
  }
}

const applyPriceAxis = (reference: number | null | undefined) => {
  if (!candleSeries) return
  const { precision, minMove } = fxPricePrecision(reference)
  if (precision === lastPricePrecision) return
  lastPricePrecision = precision
  const priceFormat = { type: 'price' as const, precision, minMove }
  candleSeries.applyOptions({ priceFormat })
  for (const period of FX_MA_PERIODS) maSeries[period]?.applyOptions({ priceFormat })
}

const buildLegend = (index: number) => {
  const candles = engine.candles
  if (!candles.length) {
    legend.value = null
    return
  }
  const i = Math.min(Math.max(index, 0), candles.length - 1)
  const c = candles[i]!
  const prev = i > 0 ? candles[i - 1]!.c : null
  const ma: Record<number, number | null> = {}
  for (const period of FX_MA_PERIODS) ma[period] = engine.maAt(period, i)
  legend.value = {
    t: c.t,
    o: c.o,
    h: c.h,
    l: c.l,
    c: c.c,
    v: c.v,
    changePct: prev !== null && prev > 0 ? ((c.c - prev) / prev) * 100 : null,
    ma,
  }
}

const applyVisibleRangeToNow = () => {
  if (!chartInstance) return
  const candles = engine.candles
  const clientNow = Math.floor(Date.now() / 1000)
  const last = candles[candles.length - 1]
  const step = INTERVAL_SECONDS[props.interval]
  const to = Math.max(clientNow, last ? last.t + step : clientNow)
  const lookback = Math.max(60, DEFAULT_VISIBLE_CANDLE_COUNT * step)
  chartInstance.timeScale().setVisibleRange({
    from: (to - lookback) as UTCTimestamp,
    to: to as UTCTimestamp,
  })
}

const renderAll = () => {
  if (!candleSeries || !volumeSeries) return
  const candles = engine.candles
  candleSeries.setData(candles.map(toCandleData))
  volumeSeries.setData(candles.map(toVolumeData))
  for (const period of FX_MA_PERIODS) {
    const series = maSeries[period]
    if (!series) continue
    const values = engine.ma(period)
    const data: LineData<UTCTimestamp>[] = []
    for (let i = 0; i < candles.length; i++) {
      const value = values[i]
      if (value === undefined || Number.isNaN(value)) continue
      data.push({ time: candles[i]!.t as UTCTimestamp, value })
    }
    series.setData(data)
  }
  candleCount.value = candles.length
  applyPriceAxis(candles[candles.length - 1]?.c)
  applyVisibleRangeToNow()
  buildLegend(candles.length - 1)
}

const applyChanged = (changed: FxCandle[]) => {
  if (!candleSeries || !volumeSeries || changed.length === 0) return
  const startIndex = engine.count - changed.length
  changed.forEach((candle, offset) => {
    const index = startIndex + offset
    candleSeries!.update(toCandleData(candle))
    volumeSeries!.update(toVolumeData(candle))
    for (const period of FX_MA_PERIODS) {
      const value = engine.maAt(period, index)
      if (value !== null) {
        maSeries[period]?.update({ time: candle.t as UTCTimestamp, value })
      }
    }
  })
  candleCount.value = engine.count
  applyPriceAxis(changed[changed.length - 1]!.c)
  if (!userScrolledBack) buildLegend(engine.count - 1)
}

const findIndexByTime = (seconds: number): number => {
  const candles = engine.candles
  let lo = 0
  let hi = candles.length - 1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (candles[mid]!.t === seconds) return mid
    if (candles[mid]!.t < seconds) lo = mid + 1
    else hi = mid - 1
  }
  return hi >= 0 ? hi : -1
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
      scaleMargins: { top: 0.12, bottom: 0.26 },
    },
    timeScale: { borderColor: '#000', timeVisible: true, secondsVisible: false },
    crosshair: { mode: 1 },
    // 右轴与 tooltip 共用同一价格 formatter（动态小数位，不固定 2 位）
    localization: { priceFormatter: (price: number) => formatFxPrice(price) },
    width: chartRef.value.clientWidth,
    height: chartRef.value.clientHeight || 420,
  })

  const palette = getPalette()
  candleSeries = chartInstance.addSeries(CandlestickSeries, {
    upColor: palette.up,
    downColor: palette.down,
    wickUpColor: palette.up,
    wickDownColor: palette.down,
    borderVisible: false,
    priceFormat: fxPricePrecision(1),
    priceLineVisible: true,
    lastValueVisible: true,
  })
  lastPricePrecision = fxPricePrecision(1).precision

  for (const period of FX_MA_PERIODS) {
    maSeries[period] = chartInstance.addSeries(LineSeries, {
      color: MA_COLORS[period] ?? '#666',
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
      priceFormat: fxPricePrecision(1),
    })
  }

  volumeSeries = chartInstance.addSeries(HistogramSeries, {
    color: '#94a3b8',
    priceFormat: { type: 'volume' },
    priceScaleId: '',
  })
  volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })

  // 用户拖时间轴看历史 → 暂停 1Hz ticker 平移（同市场页）
  chartInstance.timeScale().subscribeVisibleTimeRangeChange((range) => {
    if (!range) return
    const nowSec = Math.floor(Date.now() / 1000)
    userScrolledBack = nowSec - (range.to as number) > 2
  })

  // 十字线移动 → 图例展示该根 OHLC / MA / 成交量；移出后回到最新根
  chartInstance.subscribeCrosshairMove((param) => {
    const time = param.time as number | undefined
    if (time === undefined || !param.point) {
      buildLegend(engine.count - 1)
      return
    }
    const index = findIndexByTime(time)
    if (index >= 0) buildLegend(index)
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

const startTicker = () => {
  if (tickerId !== null) return
  tickerId = setInterval(() => {
    if (userScrolledBack) return
    applyVisibleRangeToNow()
  }, 1000)
}

const stopTicker = () => {
  if (tickerId !== null) {
    clearInterval(tickerId)
    tickerId = null
  }
}

const loadFull = async () => {
  const pairId = props.pairId
  if (!pairId) return
  loading.value = true
  error.value = null
  const gen = ++loadGen
  try {
    const to = new Date()
    const from = new Date(to.getTime() - LOOKBACK_MINUTES[props.interval] * 60_000)
    const points = await fxApi.getChart(pairId, props.interval, from.toISOString(), to.toISOString())
    if (gen !== loadGen) return
    engine = new FxCandleEngine(INTERVAL_SECONDS[props.interval], FX_MA_PERIODS)
    engine.load(points)
    await nextTick()
    if (!chartInstance) initChart()
    renderAll()
  } catch (e) {
    if (gen !== loadGen) return
    error.value = e instanceof Error ? e.message : 'K线数据加载失败'
    console.error('[FxCandleChart] loadFull failed:', e)
  } finally {
    if (gen === loadGen) loading.value = false
  }
}

const onTick = (tick: FxPriceTick | null | undefined) => {
  if (!tick) return
  const price = Number(tick.price)
  const result = engine.applyPrice(price, tick.ts)
  if (result.reload) {
    void loadFull()
    return
  }
  applyChanged(result.changed)
}

watch(
  () => props.tick,
  (tick) => onTick(tick),
)

watch(
  () => [props.pairId, props.interval],
  () => {
    void loadFull()
  },
)

watch(
  () => props.reloadToken,
  () => {
    void loadFull()
  },
)

onMounted(async () => {
  initChart()
  setupResizeObserver()
  startTicker()
  await loadFull()
})

onUnmounted(() => {
  stopTicker()
  if (resizeRafId !== null) {
    cancelAnimationFrame(resizeRafId)
    resizeRafId = null
  }
  if (resizeObserver) {
    resizeObserver.disconnect()
    resizeObserver = null
  }
  if (chartInstance) {
    chartInstance.remove()
    chartInstance = null
  }
  candleSeries = null
  volumeSeries = null
  for (const period of FX_MA_PERIODS) maSeries[period] = null
})

const formatChartTime = (seconds: number | null): string => {
  if (seconds === null) return '—'
  const d = new Date(seconds * 1000)
  if (!Number.isFinite(d.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

const legendUp = computed(() => (legend.value?.changePct ?? 0) >= 0)
</script>

<template>
  <div class="fx-candle-chart" :style="{ height: props.height }">
    <div ref="chartRef" class="fx-candle-canvas"></div>

    <div v-if="legend" class="fx-candle-legend">
      <span class="fx-legend-time">{{ formatChartTime(legend.t) }}</span>
      <span class="fx-legend-item">开 <b>{{ formatFxPrice(legend.o) }}</b></span>
      <span class="fx-legend-item">高 <b>{{ formatFxPrice(legend.h) }}</b></span>
      <span class="fx-legend-item">低 <b>{{ formatFxPrice(legend.l) }}</b></span>
      <span class="fx-legend-item">收 <b>{{ formatFxPrice(legend.c) }}</b></span>
      <span v-if="legend.changePct !== null" class="fx-legend-item" :class="legendUp ? 'up' : 'down'">
        {{ legendUp ? '+' : '' }}{{ legend.changePct.toFixed(2) }}%
      </span>
      <span class="fx-legend-item">量 <b>{{ formatFxAmount(legend.v) }}</b></span>
      <span v-for="period in FX_MA_PERIODS" :key="period" class="fx-legend-item">
        <i class="fx-legend-dot" :style="{ background: MA_COLORS[period] }"></i>
        MA{{ period }} <b>{{ formatFxPrice(legend.ma[period]) }}</b>
      </span>
    </div>

    <div v-if="loading && candleCount === 0" class="fx-candle-overlay">K 线加载中…</div>
    <div v-else-if="error && candleCount === 0" class="fx-candle-overlay">
      <span class="fx-candle-overlay-error">{{ error }}</span>
      <button class="fx-candle-retry" @click="loadFull">重试</button>
    </div>
    <div v-else-if="!loading && !error && candleCount === 0" class="fx-candle-overlay">
      暂无成交，等待第一笔交易
    </div>
  </div>
</template>

<style scoped>
.fx-candle-chart {
  position: relative;
  width: 100%;
  min-height: 260px;
}
.fx-candle-canvas {
  width: 100%;
  height: 100%;
}
.fx-candle-legend {
  position: absolute;
  top: 6px;
  left: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  max-width: calc(100% - 90px);
  padding: 2px 6px;
  background: rgba(255, 255, 255, 0.86);
  border: 1px solid #ddd;
  font-family: ui-monospace, monospace;
  font-size: 11px;
  line-height: 1.5;
  color: #333;
  pointer-events: none;
  z-index: 2;
}
.fx-legend-time {
  color: #888;
}
.fx-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  white-space: nowrap;
}
.fx-legend-item b {
  font-weight: 700;
}
.fx-legend-item.up { color: var(--color-up, #16a34a); }
.fx-legend-item.down { color: var(--color-down, #dc2626); }
.fx-legend-dot {
  display: inline-block;
  width: 8px;
  height: 2px;
}
.fx-candle-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  align-items: center;
  justify-content: center;
  background: rgba(255, 255, 255, 0.85);
  color: #666;
  font-size: 13px;
  z-index: 3;
}
.fx-candle-overlay-error {
  color: var(--color-down, #dc2626);
  font-weight: 600;
}
.fx-candle-retry {
  border: 2px solid #000;
  background: #000;
  color: #fff;
  padding: 4px 14px;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}
</style>
