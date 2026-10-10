<script setup lang="ts">
// FX 专用 K 线组件：复用市场页 CandleChart 的成熟行为（forming candle、空桶填充、
// 增量 MA、drag 暂停 ticker、resize、gap reload），数据源换成封存段/SSE 尾段适配层
// （`useFxCandleHistory`）与页面转发的公开信封。
//
// 实时增量只走「真实成交」：页面唯一 `FxStream.onEnvelope` 转发 `FxPublicEnvelope`，
// 组件用 `decodeFxTradeTick` + `engine.applyTrades` 按真实成交时间与金侧成交量更新
// OHLCV；quote-only 帧只更新页面报价条，不伪造成交。`tick`/`applyPrice` 仅保留给
// 没有 envelope 的旧调用方，不与真实成交路径并行使用。
//
// 覆盖游标纪律：加载期间到达的实时成交先进入有界缓冲，`engine.load(points,
// throughTradeId)` 先写入覆盖游标，再按游标补放缓冲（重复 id 自动跳过），保证
// 成交量不重复、不漏记。快照尾段只在「同一帧携带的覆盖游标 + 尚未应用更新成交」
// 时用于加载；一旦有实时成交超过尾段游标，就改用 `/chart` 的 body+游标同源回退。
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
import { formatFxAmount, formatFxPrice, fxPricePrecision } from '@/api/fx'
import type {
  FxChartInterval,
  FxChartPoint,
  FxHistorySnapshotTail,
  FxPriceTick,
  FxPublicEnvelope,
  FxTradeTick,
} from '@/types/fx'
import {
  FxCandleEngine,
  FX_MA_PERIODS,
  fxTimestampToSeconds,
  fxCandleVisibleRange,
  fxCandleScrolledBack,
  type FxCandle,
  type FxCandleTrade,
} from '@/utils/fxCandle'
import {
  FX_HISTORY_SEGMENT_SECONDS,
  decodeFxTradeTick,
  mergeFxHistoryCandles,
} from '@/utils/fxHistory'
import {
  loadFxHistoryResult,
  loadFxHistoryTailResult,
} from '@/composables/useFxCandleHistory'
import { getPalette, withAlpha } from '@/utils/palette'

const props = withDefaults(
  defineProps<{
    pairId: number
    interval?: FxChartInterval
    /**
     * 页面唯一 `FxStream.onEnvelope` 转发的最新公开信封。组件自行消费历史版本/
     * 尾段元数据与逐笔真实成交；quote-only 帧不产生任何成交。
     */
    envelope?: FxPublicEnvelope | null
    /** 旧兼容：无 envelope 的调用方用纯价格 tick（applyPrice）；有 envelope 时忽略。 */
    tick?: FxPriceTick | null
    /** 递增：重连/手动刷新/成交后优先补尾段（不重读封存段）。 */
    reloadToken?: number
    height?: string
  }>(),
  {
    interval: '1m',
    envelope: null,
    tick: null,
    reloadToken: 0,
    height: '100%',
  },
)

const LOOKBACK_MINUTES: Record<FxChartInterval, number> = { '1m': 480, '15m': 1200, '1h': 4800 }
const INTERVAL_SECONDS: Record<FxChartInterval, number> = { '1m': 60, '15m': 900, '1h': 3600 }
const MA_COLORS: Record<number, string> = { 10: '#f59e0b', 20: '#2563eb' }
/** 加载/补尾期间允许缓冲的实时成交上限；超过则放弃缓冲并请求补尾段，绝不伪造零成交。 */
const FX_MAX_PENDING_TRADES = 5000

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

// ── 历史 / 尾段 / 实时成交状态 ──
/** 由信封维护的最新历史上下文；尾段与其覆盖游标必须来自同一帧。 */
let snapshotTail: FxHistorySnapshotTail | null = null
/** 最近一次发起完整加载时的版本/就绪 key，避免失败后每帧重试。 */
let attemptedKey: string | null = null
/** 上次完整加载得到的封存边界（epoch 秒）与边界前的不可变点。 */
let sealedBoundary: number | null = null
let sealedPoints: FxChartPoint[] = []
let loadedFromSec = 0
let loadedVersion: string | null = null
/** 正在异步加载（完整或补尾段）时缓冲实时成交，加载完成后按覆盖游标补放一次。 */
let fullLoading = false
let refreshing = false
let queuedTailRefresh = false
let pendingTrades: FxCandleTrade[] = []
let pendingOverflow = false

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
  const range = fxCandleVisibleRange(
    engine.candles, Math.floor(Date.now() / 1000), INTERVAL_SECONDS[props.interval],
  )
  if (!range) return
  chartInstance.timeScale().setVisibleRange({
    from: range.from as UTCTimestamp,
    to: range.to as UTCTimestamp,
  })
}

const renderAll = (resetRange = true) => {
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
  if (resetRange) applyVisibleRangeToNow()
  buildLegend(candles.length - 1)
}

/**
 * 增量推送给 lightweight-charts。
 * `applyTrades` 的 `changed` 可能是「尾部连续区间」，也可能只是更早的若干桶
 * （乱序/补尾）。`update()` 只能改最后一根或追加，因此尾部连续时用 update，
 * 否则整段 `setData`，绝不把错位的 candle 推给图表。
 */
const applyChanged = (changed: FxCandle[]) => {
  if (!candleSeries || !volumeSeries || changed.length === 0) return
  const wasEmpty = candleCount.value === 0
  const candles = engine.candles
  const startIndex = engine.count - changed.length
  const trailing = startIndex >= 0
    && changed.every((candle, offset) => candles[startIndex + offset]?.t === candle.t)
  if (!trailing) {
    renderAll(!userScrolledBack)
    return
  }
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
  if (wasEmpty) {
    userScrolledBack = false
    applyVisibleRangeToNow()
  }
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

/** 立即清空渲染序列（不改变缓冲/加载标志），供 pair/周期切换与历史版本切换用。 */
const clearRenderedSeries = () => {
  candleSeries?.setData([])
  volumeSeries?.setData([])
  for (const period of FX_MA_PERIODS) maSeries[period]?.setData([])
  candleCount.value = 0
  legend.value = null
}

/**
 * 同步作废当前图与覆盖：新 pair/周期或新 history_version 时旧序列不再代表当前数据。
 * 只保留实时成交缓冲（同 pair 的全局成交 id 仍有效），由随后加载按新覆盖游标补放。
 */
const resetChartState = () => {
  engine = new FxCandleEngine(INTERVAL_SECONDS[props.interval], FX_MA_PERIODS)
  sealedBoundary = null
  sealedPoints = []
  loadedFromSec = 0
  loadedVersion = null
  clearRenderedSeries()
}

// ── 覆盖游标 / 尾段上下文 ──

/** 当前时间的封存边界（与后端 segment 对齐）。 */
const sealedBoundaryForNow = (): number => {
  const segment = FX_HISTORY_SEGMENT_SECONDS[props.interval]
  const nowSec = Math.floor(Date.now() / 1000)
  return nowSec - (nowSec % segment)
}

/** 版本 + 就绪 组成的加载 key；同一 key 加载失败不逐帧重试。 */
const currentLoadKey = (): string => {
  const version = snapshotTail?.history_version ?? ''
  const ready = snapshotTail?.history_ready === undefined ? '' : String(snapshotTail.history_ready)
  return `${version}|${ready}`
}

/**
 * 快照尾段能否安全用于加载：尾段与其覆盖游标来自同一帧；一旦有实时成交超过
 * 该游标，尾段就不再包含这些成交，必须改用 `/chart` 的 body+游标同源回退。
 */
const tailUsableForLoad = (): boolean => {
  if (!snapshotTail?.history_tail) return false
  // 适配层可能因封存段/HTTP 版本不一致而整窗重建：重建后 SSE 版本落后于已加载版本，
  // 此时绝不再用旧版本尾段与旧封存点拼接。
  if (loadedVersion && snapshotTail.history_version && snapshotTail.history_version !== loadedVersion) return false
  const coverage = snapshotTail.history_tail_through_trade_id
  const applied = engine.throughTradeId
  if (applied === null) return true
  return typeof coverage === 'number' && applied <= coverage
}

/** 取本次加载使用的尾段上下文；需要走 HTTP 时清掉尾段，避免后续复用过期段。 */
const tailForLoad = (forceHttp: boolean): FxHistorySnapshotTail | null => {
  if (!snapshotTail) return null
  if (forceHttp || !tailUsableForLoad()) {
    snapshotTail = {
      ...snapshotTail,
      history_tail: null,
      history_tail_at: null,
      history_tail_through_trade_id: null,
    }
  }
  return snapshotTail
}

function syncTailFromEnvelope(env: FxPublicEnvelope): void {
  const previous = snapshotTail
  const previousVersion = previous?.history_version ?? null
  const versionChanged = !!env.history_version && env.history_version !== previousVersion
  if (!previous || versionChanged) {
    snapshotTail = {
      history_version: env.history_version ?? null,
      history_tail: env.history_tail ?? null,
      history_tail_at: env.history_tail_at ?? null,
      history_tail_through_trade_id: typeof env.history_tail_through_trade_id === 'number'
        ? env.history_tail_through_trade_id : null,
      history_ready: env.history_ready,
    }
    return
  }
  if (env.history_tail) {
    // 新快照同时带来尾段与游标：两者必须成对更新。
    snapshotTail = {
      ...previous,
      history_version: env.history_version ?? previous.history_version,
      history_tail: env.history_tail,
      history_tail_at: env.history_tail_at ?? null,
      history_tail_through_trade_id: typeof env.history_tail_through_trade_id === 'number'
        ? env.history_tail_through_trade_id : null,
      history_ready: env.history_ready ?? previous.history_ready,
    }
    return
  }
  // 增量帧不带尾段：只更新版本/就绪，绝不单独推进尾段覆盖游标——否则旧段配新游标
  // 会在重载时丢掉已应用的实时成交。
  snapshotTail = {
    ...previous,
    history_version: env.history_version ?? previous.history_version,
    history_ready: env.history_ready ?? previous.history_ready,
  }
}

function markTailInvalidated(): void {
  if (!snapshotTail) return
  snapshotTail = {
    ...snapshotTail,
    history_tail: null,
    history_tail_at: null,
    history_tail_through_trade_id: null,
  }
}

// ── 实时成交缓冲与应用 ──

function applyTradeBatch(trades: FxCandleTrade[]): void {
  if (trades.length === 0) return
  const result = engine.applyTrades(trades)
  if (result.reload) {
    // 缺口/乱序无法本地判定：补尾段（必要时 upgrade 为完整重读），绝不猜时序。
    void refreshTail(false)
    return
  }
  applyChanged(result.changed)
}

function drainPendingTrades(): void {
  if (pendingOverflow) {
    pendingOverflow = false
    pendingTrades = []
    void refreshTail(true)
    return
  }
  if (pendingTrades.length === 0) return
  const batch = pendingTrades
  pendingTrades = []
  applyTradeBatch(batch)
}

function enqueueTrades(ticks: readonly FxTradeTick[]): void {
  if (pendingOverflow) return
  const decoded: FxCandleTrade[] = []
  for (const tick of ticks) {
    const trade = decodeFxTradeTick(tick)
    if (trade) decoded.push(trade)
  }
  if (decoded.length === 0) return
  if (refreshing) {
    if (pendingTrades.length + decoded.length > FX_MAX_PENDING_TRADES) {
      pendingOverflow = true
      pendingTrades = []
      return
    }
    pendingTrades.push(...decoded)
    return
  }
  applyTradeBatch(decoded)
}

// ── 加载 ──

async function loadFull(): Promise<void> {
  const pairId = props.pairId
  if (!pairId) return
  const gen = ++loadGen
  attemptedKey = currentLoadKey()
  fullLoading = true
  refreshing = true
  loading.value = true
  error.value = null
  try {
    const result = await loadFxHistoryResult(
      pairId, props.interval, LOOKBACK_MINUTES[props.interval], tailForLoad(false),
    )
    if (gen !== loadGen) return
    const stepSec = INTERVAL_SECONDS[props.interval]
    const nowSec = Math.floor(Date.now() / 1000)
    const boundary = sealedBoundaryForNow()
    const firstEpoch = result.points.length > 0 ? fxTimestampToSeconds(result.points[0]!.t) : null
    loadedFromSec = firstEpoch ?? nowSec - LOOKBACK_MINUTES[props.interval] * 60
    sealedBoundary = boundary
    sealedPoints = result.points.filter((point) => {
      const epoch = fxTimestampToSeconds(point.t)
      return epoch !== null && epoch < boundary
    })
    loadedVersion = result.historyVersion
    engine = new FxCandleEngine(stepSec, FX_MA_PERIODS)
    // 覆盖游标必须先于缓冲的实时成交写入，重复 id 随游标被跳过。
    engine.load(result.points, result.throughTradeId)
    await nextTick()
    if (!chartInstance) initChart()
    renderAll()
  } catch (e) {
    if (gen !== loadGen) return
    error.value = e instanceof Error ? e.message : 'K线数据加载失败'
    console.error('[FxCandleChart] loadFull failed:', e)
  } finally {
    if (gen === loadGen) {
      fullLoading = false
      refreshing = false
      loading.value = false
      if (!error.value) drainPendingTrades()
      if (queuedTailRefresh) {
        queuedTailRefresh = false
        if (!error.value) void refreshTail(false)
      }
    }
  }
}

async function refreshTail(forceHttp: boolean): Promise<void> {
  const pairId = props.pairId
  if (!pairId) return
  if (fullLoading || refreshing) {
    queuedTailRefresh = true
    return
  }
  const boundary = sealedBoundaryForNow()
  if (sealedBoundary === null || boundary !== sealedBoundary || !loadedVersion) {
    // 封存段已翻页或尚未完整加载：兼容尾段不可用，走完整重读。
    await loadFull()
    return
  }
  const gen = ++loadGen
  refreshing = true
  try {
    const result = await loadFxHistoryTailResult(pairId, props.interval, tailForLoad(forceHttp))
    if (gen !== loadGen) return
    if (result.historyVersion !== loadedVersion) {
      // 尾段属于新的 history_version：旧 sealedPoints/旧尾段整代作废，绝不拼接。
      // 强制 /chart 整窗自洽重读；缓冲保留，由 loadFull 的 finally/generation 收尾。
      if (snapshotTail) {
        snapshotTail = {
          ...snapshotTail,
          history_tail: null,
          history_tail_at: null,
          history_tail_through_trade_id: null,
        }
      }
      resetChartState()
      await loadFull()
      return
    }
    const stepSec = INTERVAL_SECONDS[props.interval]
    const nowSec = Math.floor(Date.now() / 1000)
    // 对齐的 exclusive 结束桶：now 未对齐时不产出一个未来空桶（与适配层一致）。
    const endExclusive = nowSec - (nowSec % stepSec) + stepSec
    // 只替换 [封存边界, now) 的尾巴，封存点原样保留。
    const points = mergeFxHistoryCandles(sealedPoints, result.points, stepSec, loadedFromSec, endExclusive)
    engine.load(points, result.throughTradeId)
    loadedVersion = result.historyVersion
    error.value = null
    renderAll()
  } catch (e) {
    if (gen !== loadGen) return
    console.warn('[FxCandleChart] tail refresh failed, reloading history:', e)
    await loadFull()
    return
  } finally {
    if (gen === loadGen) {
      refreshing = false
      if (!error.value) drainPendingTrades()
      if (queuedTailRefresh) {
        queuedTailRefresh = false
        if (!error.value) void refreshTail(false)
      }
    }
  }
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
    userScrolledBack = fxCandleScrolledBack(engine.candles, range.to as number)
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

const onTick = (tick: FxPriceTick | null | undefined) => {
  // 有 envelope 时只走真实成交路径，绝不并行混用 applyPrice（会改写同一桶 OHLC）。
  if (props.envelope || !tick) return
  const price = Number(tick.price)
  const result = engine.applyPrice(price, tick.ts)
  if (result.reload) {
    void loadFull()
    return
  }
  applyChanged(result.changed)
}

watch(
  () => props.envelope,
  (env) => {
    if (!env) return
    syncTailFromEnvelope(env)
    if (currentLoadKey() !== attemptedKey) {
      // 只有 history_version 真正变化才作废旧序列；同版本 ready 翻转仍保留旧数据。
      if ((snapshotTail?.history_version ?? null) !== loadedVersion) resetChartState()
      void loadFull()
    }
    if (env.history_invalidated) {
      markTailInvalidated()
      void refreshTail(true)
    }
    if (env.trades && env.trades.length > 0) enqueueTrades(env.trades)
  },
  { flush: 'sync' },
)

watch(
  () => props.tick,
  (tick) => onTick(tick),
)

watch(
  () => [props.pairId, props.interval],
  () => {
    // 新 pair/周期：旧图与旧覆盖同步作废，避免切换失败时仍显示上一个 pair 的 K 线。
    snapshotTail = null
    attemptedKey = null
    pendingTrades = []
    pendingOverflow = false
    queuedTailRefresh = false
    resetChartState()
    void loadFull()
  },
)

watch(
  () => props.reloadToken,
  () => {
    void refreshTail(false)
  },
)

onMounted(async () => {
  // 挂载前已到达的信封不会再触发 watcher：先吸收一次，避免首屏只能走 /chart。
  if (props.envelope) syncTailFromEnvelope(props.envelope)
  initChart()
  setupResizeObserver()
  startTicker()
  // 先启动加载（同步置 refreshing），再把挂载前信封里的真实成交放入缓冲；
  // 这样它们会在覆盖游标写入后按 id 过滤补放，绝不漏量。
  const initial = loadFull()
  if (props.envelope?.trades?.length) enqueueTrades(props.envelope.trades)
  await initial
})

onUnmounted(() => {
  loadGen++
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

    <div v-if="error && candleCount > 0" class="fx-candle-stale" role="alert">
      <span>{{ error }}</span>
      <button class="fx-candle-retry" @click="loadFull">重试</button>
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
.fx-candle-stale {
  position: absolute;
  top: 6px;
  right: 8px;
  display: flex;
  align-items: center;
  gap: 6px;
  max-width: 70%;
  padding: 2px 6px;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid var(--color-down, #dc2626);
  color: var(--color-down, #dc2626);
  font-size: 11px;
  line-height: 1.5;
  z-index: 2;
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
