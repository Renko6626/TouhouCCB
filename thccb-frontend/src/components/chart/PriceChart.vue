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
import type { PricePoint } from '@/types/api'
import { getPalette } from '@/utils/palette'
import { MarketRealtimeKey } from '@/composables/useMarketRealtime'

const props = withDefaults(defineProps<{
  outcomeId: number
  lookbackMinutes?: number
  width?: string
  height?: string
}>(), {
  lookbackMinutes: 1440,
  width: '100%',
  height: '400px',
})

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

// 按 lookback 选 bucket 降采样：避免长窗口下区间交易笔数撑爆后端 5000 行硬上限
// （chart.py:_fetch_initial_shares_and_replay 的 `len(replay_rows) > limit` check）。
// 同时控制返回点数 ≤ ~1500，前端绘制压力小。1h 留逐笔以保实时观感。
const PRICE_BUCKET_THRESHOLDS: { maxLookbackMin: number; bucket?: string }[] = [
  { maxLookbackMin: 60,    bucket: undefined }, // ≤1h：逐笔
  { maxLookbackMin: 360,   bucket: '30s' },     // ≤6h
  { maxLookbackMin: 1440,  bucket: '5m' },      // ≤24h
  { maxLookbackMin: 4320,  bucket: '15m' },     // ≤3d
  { maxLookbackMin: 10080, bucket: '1h' },      // ≤7d
]
const pickBucket = (lookbackMin: number): string | undefined => {
  for (const row of PRICE_BUCKET_THRESHOLDS) {
    if (lookbackMin <= row.maxLookbackMin) return row.bucket
  }
  return '1h'
}

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
      secondsVisible: props.lookbackMinutes <= 60,
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

// 全量加载（初始 / 切换 outcome+lookback / gap reconcile / 重连）
const loadFull = async () => {
  if (!props.outcomeId) return
  loading.value = true
  error.value = null
  try {
    const now = new Date()
    const fromTs = new Date(now.getTime() - props.lookbackMinutes * 60 * 1000).toISOString()
    const toTs = now.toISOString()
    // limit=20000 是后端 hard cap（chart.py 的 Query(ge=1, le=20000)）。
    // bucket 仅压缩返回点数，不能减少 replay_rows 数；所以 limit 拉满 + bucket 双管齐下。
    const bucket = pickBucket(props.lookbackMinutes)
    const resp = await chartApi.getPriceSeries(props.outcomeId, fromTs, toTs, 20000, bucket)
    if (!resp || !resp.points) {
      pointCount.value = 0
      return
    }

    const points = resp.points
    pointCount.value = points.length
    firstPrice.value = points[0]?.price ?? null
    lastPrice.value = points[points.length - 1]?.price ?? null

    await nextTick()
    if (!chartInstance) initChart()
    if (!areaSeries) return

    const fromTsSec = Math.floor(new Date(fromTs).getTime() / 1000) as UTCTimestamp
    const toTsSec = Math.floor(now.getTime() / 1000) as UTCTimestamp

    const data = points.map((pt: PricePoint) => ({
      time: Math.floor(new Date(pt.ts).getTime() / 1000) as UTCTimestamp,
      value: pt.price,
    }))
    // 追加一个"现在"的合成端点，价格沿用最后一笔成交（LMSR 无成交期间价格恒定）。
    // 这样即使最后一笔在 1h 前，曲线也会延伸到当前时刻，配合 setVisibleRange 才能
    // 让用户看到完整的请求时间窗。SSE 增量更新走 update()，同 ts 会自动 replace。
    if (data.length > 0) {
      const last = data[data.length - 1]!
      if ((last.time as number) < (toTsSec as number)) {
        data.push({ time: toTsSec, value: last.value })
      }
    }
    areaSeries.setData(data)
    lastWrittenTs = data.length > 0 ? (data[data.length - 1]!.time as number) : 0
    applyDirectionColors()

    // 不用 fitContent()——它会缩到数据范围，让 1h 和 7d lookback 在稀疏交易时表现一致。
    // setVisibleRange 强制时间轴跨整个请求窗口，曲线短就让左侧留空，时间尺度诚实。
    if (data.length > 0) {
      chartInstance?.timeScale().setVisibleRange({ from: fromTsSec, to: toTsSec })
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '价格数据加载失败'
    console.error('[PriceChart] loadFull failed:', err)
    // 切到新 lookback 失败时清掉旧数据，避免 chart 残留上一档 lookback 的图但视窗
    // 被 1Hz ticker 拉宽到新 lookback —— 这就是"显示不出来"的视觉来源。清空后
    // hasData=false 才会让 error overlay 走 v-else-if 显示出来。
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
    const lookbackSec = Math.max(60, props.lookbackMinutes * 60)
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

// 切换 outcome / lookback → 完全重载
watch(() => [props.outcomeId, props.lookbackMinutes], () => {
  chartInstance?.applyOptions({
    timeScale: { secondsVisible: props.lookbackMinutes <= 60 },
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
