<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  createChart,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import {
  computeMinOut,
  formatFxAmount,
  fxApi,
  FxOrderSubmitter,
  fxOrderSignature,
  FxStream,
  isConflictError,
  mapFxError,
  tradeSlippageBps,
} from '@/api/fx'
import { userApi } from '@/api/user'
import type {
  FxChartPoint,
  FxPairPublic,
  FxPublicFrame,
  FxPublicNews,
  FxQuote,
  FxSide,
  FxSnapshot,
  FxTradePublic,
  FxWalletPublic,
} from '@/types/fx'
import { getPalette, withAlpha } from '@/utils/palette'

defineOptions({ name: 'FxPage' })

const msg = useMessage()

type FxInterval = '1m' | '15m' | '1h'
const INTERVAL_SECONDS: Record<FxInterval, number> = { '1m': 60, '15m': 900, '1h': 3600 }
const LOOKBACK_MINUTES: Record<FxInterval, number> = { '1m': 480, '15m': 1200, '1h': 4800 }

interface FxDisplaySummary {
  fx_mtm: number
  fx_cost_basis: number
  fx_unrealized_pnl: number
}

const loading = ref(true)
const error = ref<string | null>(null)
const pairs = ref<FxPairPublic[]>([])
const pairId = ref<number | null>(null)
const snapshot = ref<FxSnapshot | null>(null)
const trades = ref<FxTradePublic[]>([])
const wallet = ref<FxWalletPublic | null>(null)
const newsFeed = ref<FxPublicNews[]>([])
const streamConnected = ref(false)

const intervals: FxInterval[] = ['1m', '15m', '1h']
const interval = ref<FxInterval>('1m')
const candleCount = ref(0)
const candles = ref<FxChartPoint[]>([])

const side = ref<FxSide>('buy')
const amount = ref('')
const slippageBps = ref<number>(50)
const quote = ref<FxQuote | null>(null)
const quoting = ref(false)
const submitting = ref(false)
const tradeError = ref<string | null>(null)
const orderSubmitter = new FxOrderSubmitter()

const summary = ref<FxDisplaySummary | null>(null)

const activePair = computed(() => pairs.value.find((p) => p.id === pairId.value) ?? null)
const tradable = computed(() => activePair.value?.status === 'trading')
const currencyName = computed(() => activePair.value?.currency_name ?? '外币')

const amountValid = computed(() => /^\d+(\.\d{0,6})?$/.test(amount.value.trim()) && Number(amount.value) > 0)
const effectiveSlippageBps = computed(() => {
  const v = Number(slippageBps.value)
  if (!Number.isFinite(v)) return 0
  return Math.max(0, Math.min(10000, Math.trunc(v)))
})
const minOut = computed(() =>
  quote.value ? computeMinOut(quote.value.output_amount, effectiveSlippageBps.value) : '',
)
// 后端 effective_price = output/input：buy 是「外币/金」，sell 是「金/外币」。
// 滑点统一折算成「金/外币」再与 snapshot.price（边际汇率，金/外币）比较。
const effectiveGoldPerForeign = computed(() => {
  const q = quote.value
  if (!q) return null
  const e = Number(q.effective_price)
  if (!Number.isFinite(e) || e <= 0) return null
  return side.value === 'buy' ? 1 / e : e
})
const quoteSlippage = computed(() =>
  effectiveGoldPerForeign.value !== null && snapshot.value
    ? tradeSlippageBps(snapshot.value.price, effectiveGoldPerForeign.value)
    : null,
)
const fxPnlPositive = computed(() => (summary.value?.fx_unrealized_pnl ?? 0) >= 0)
const walletAvgCost = computed(() => {
  const w = wallet.value
  if (!w) return null
  const amount = Number(w.foreign_amount)
  const cost = Number(w.cost_basis)
  if (!Number.isFinite(amount) || amount <= 0 || !Number.isFinite(cost)) return null
  return cost / amount
})
const priceDirection = computed(() => {
  if (candles.value.length < 2) return 'neutral'
  const first = candles.value[0]!.o
  const last = candles.value[candles.value.length - 1]!.c
  if (last > first) return 'up'
  if (last < first) return 'down'
  return 'neutral'
})

// ── 图表 ──
const chartRef = ref<HTMLDivElement | null>(null)
const chartLoading = ref(false)
let chartInstance: IChartApi | null = null
let candleSeries: ISeriesApi<'Candlestick', Time> | null = null
let volumeSeries: ISeriesApi<'Histogram', Time> | null = null
let resizeObserver: ResizeObserver | null = null

const toTimestamp = (iso: string): UTCTimestamp =>
  Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp

function ensureChart() {
  if (chartInstance || !chartRef.value) return
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
    timeScale: { borderColor: '#000', timeVisible: true },
    crosshair: { mode: 1 },
    width: chartRef.value.clientWidth,
    height: chartRef.value.clientHeight || 360,
  })
  const palette = getPalette()
  candleSeries = chartInstance.addSeries(CandlestickSeries, {
    upColor: palette.up,
    downColor: palette.down,
    wickUpColor: palette.up,
    wickDownColor: palette.down,
    borderVisible: false,
  })
  volumeSeries = chartInstance.addSeries(HistogramSeries, {
    color: '#94a3b8',
    priceFormat: { type: 'volume' },
    priceScaleId: '',
  })
  volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })
  if (typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (!entry || !chartInstance) return
      chartInstance.applyOptions({
        width: entry.contentRect.width,
        height: entry.contentRect.height,
      })
    })
    resizeObserver.observe(chartRef.value)
  }
}

function renderCandles(points: FxChartPoint[]) {
  if (!candleSeries || !volumeSeries) return
  const palette = getPalette()
  const candleData: CandlestickData<UTCTimestamp>[] = points.map((c) => ({
    time: toTimestamp(c.t),
    open: c.o,
    high: c.h,
    low: c.l,
    close: c.c,
  }))
  const volumeData: HistogramData<UTCTimestamp>[] = points.map((c) => ({
    time: toTimestamp(c.t),
    value: c.v,
    color: withAlpha(c.c >= c.o ? palette.up : palette.down, 0x80),
  }))
  candleSeries.setData(candleData)
  volumeSeries.setData(volumeData)
  candleCount.value = points.length
  chartInstance?.timeScale().fitContent()
}

function applyFrameToChart(price: number) {
  if (!candleSeries || !Number.isFinite(price) || price <= 0) return
  const step = INTERVAL_SECONDS[interval.value]
  const bucket = Math.floor(Date.now() / 1000 / step) * step
  const last = candles.value[candles.value.length - 1]
  if (!last) {
    const point: FxChartPoint = {
      t: new Date(bucket * 1000).toISOString(),
      o: price,
      h: price,
      l: price,
      c: price,
      v: 0,
    }
    candles.value = [point]
    candleSeries.update({ time: bucket as UTCTimestamp, open: price, high: price, low: price, close: price })
    return
  }
  const lastTs = Math.floor(new Date(last.t).getTime() / 1000)
  if (bucket === lastTs) {
    last.h = Math.max(last.h, price)
    last.l = Math.min(last.l, price)
    last.c = price
    candleSeries.update({
      time: lastTs as UTCTimestamp,
      open: last.o,
      high: last.h,
      low: last.l,
      close: last.c,
    })
  } else if (bucket > lastTs) {
    const point: FxChartPoint = {
      t: new Date(bucket * 1000).toISOString(),
      o: last.c,
      h: Math.max(last.c, price),
      l: Math.min(last.c, price),
      c: price,
      v: 0,
    }
    candles.value = [...candles.value, point]
    candleSeries.update({
      time: bucket as UTCTimestamp,
      open: point.o,
      high: point.h,
      low: point.l,
      close: point.c,
    })
  } else {
    last.c = price
    candleSeries.update({
      time: lastTs as UTCTimestamp,
      open: last.o,
      high: last.h,
      low: last.l,
      close: last.c,
    })
  }
}

// ── 数据加载 ──
async function loadPairs() {
  pairs.value = await fxApi.listPairs()
  const preferred =
    pairs.value.find((p) => p.status === 'trading') ??
    pairs.value.find((p) => p.status !== 'draft') ??
    pairs.value[0]
  if (preferred) pairId.value = preferred.id
}

async function loadSnapshot() {
  const pid = pairId.value
  if (!pid) return
  snapshot.value = await fxApi.getSnapshot(pid)
}

async function loadTrades() {
  const pid = pairId.value
  if (!pid) return
  try {
    trades.value = await fxApi.getMyTrades(pid, 50)
  } catch {
    // 个人成交历史失败不阻塞行情与交易主流程
    trades.value = []
  }
}

async function loadWallet() {
  const pid = pairId.value
  if (!pid) return
  try {
    wallet.value = await fxApi.getWallet(pid)
  } catch {
    // 钱包读取失败时仅隐藏持仓明细，交易与行情仍可用
    wallet.value = null
  }
}

async function loadSummary() {
  try {
    const raw = (await userApi.getSummary()) as unknown as Partial<FxDisplaySummary>
    summary.value = {
      fx_mtm: Number(raw.fx_mtm) || 0,
      fx_cost_basis: Number(raw.fx_cost_basis) || 0,
      fx_unrealized_pnl: Number(raw.fx_unrealized_pnl) || 0,
    }
  } catch {
    // 净值面板失败不阻塞交易主流程
  }
}

async function loadChart() {
  const pid = pairId.value
  if (!pid) return
  chartLoading.value = true
  try {
    const to = new Date()
    const from = new Date(to.getTime() - LOOKBACK_MINUTES[interval.value] * 60_000)
    const points = await fxApi.getChart(pid, interval.value, from.toISOString(), to.toISOString())
    candles.value = points
    await nextTick()
    ensureChart()
    renderCandles(points)
  } catch (e) {
    // 图表失败不影响行情与交易
    console.error('[Fx] loadChart failed', e)
  } finally {
    chartLoading.value = false
  }
}

async function refreshAll() {
  await Promise.allSettled([loadSnapshot(), loadTrades(), loadWallet(), loadSummary(), loadChart()])
}

// ── SSE ──
let stream: FxStream | null = null
function onFrame(frame: FxPublicFrame) {
  if (frame.price === undefined && frame.news === undefined) return
  const current = snapshot.value
  if (current) {
    snapshot.value = {
      ...current,
      price: frame.price ?? current.price,
      buy_price: frame.buy_price ?? current.buy_price,
      sell_price: frame.sell_price ?? current.sell_price,
      spread: frame.spread ?? current.spread,
      volume_24h: frame.volume ?? current.volume_24h,
    }
  }
  if (frame.price !== undefined) applyFrameToChart(Number(frame.price))
  if (frame.news) {
    const signature = `${frame.news.published_at ?? ''}|${frame.news.title ?? ''}`
    const exists = newsFeed.value.some(
      (n) => `${n.published_at ?? ''}|${n.title ?? ''}` === signature,
    )
    if (!exists) newsFeed.value = [frame.news, ...newsFeed.value].slice(0, 30)
  }
}

function connectStream() {
  const pid = pairId.value
  if (!pid) return
  if (!stream) {
    stream = new FxStream()
    stream.onOpen(() => {
      streamConnected.value = true
    })
    stream.onError(() => {
      streamConnected.value = false
    })
    stream.onFrame(onFrame)
  }
  stream.connect(pid)
}

async function selectPair(id: number) {
  pairId.value = id
  quote.value = null
  tradeError.value = null
  newsFeed.value = []
  candles.value = []
  wallet.value = null
  try {
    await Promise.all([loadSnapshot(), loadTrades(), loadWallet(), loadChart()])
    connectStream()
  } catch (e) {
    error.value = mapFxError(e, 'FX 行情加载失败')
  }
}

async function load() {
  loading.value = true
  error.value = null
  try {
    await loadPairs()
    if (pairId.value === null) return
    await Promise.all([loadSnapshot(), loadTrades(), loadWallet(), loadSummary(), loadChart()])
    connectStream()
  } catch (e) {
    error.value = mapFxError(e, 'FX 行情加载失败')
  } finally {
    loading.value = false
  }
}

// ── 报价与成交 ──
let quoteTimer: ReturnType<typeof setTimeout> | null = null
let quoteGen = 0

function scheduleQuote() {
  quote.value = null
  if (quoteTimer) clearTimeout(quoteTimer)
  quoteTimer = setTimeout(() => {
    void fetchQuote()
  }, 350)
}

async function fetchQuote() {
  const pid = pairId.value
  const value = amount.value.trim()
  if (!pid || !tradable.value || !amountValid.value) {
    quote.value = null
    return
  }
  const gen = ++quoteGen
  quoting.value = true
  try {
    const q = await fxApi.getQuote(pid, { side: side.value, amount: value })
    if (gen === quoteGen) {
      quote.value = q
      tradeError.value = null
    }
  } catch (e) {
    if (gen === quoteGen) {
      quote.value = null
      tradeError.value = mapFxError(e, '报价失败')
    }
  } finally {
    if (gen === quoteGen) quoting.value = false
  }
}

async function submitTrade() {
  const pid = pairId.value
  if (!pid || submitting.value) return
  if (!tradable.value) {
    tradeError.value = '该货币对当前暂停或未开市，无法交易'
    return
  }
  if (!amountValid.value) {
    tradeError.value = '请输入有效的正数金额（最多 6 位小数）'
    return
  }
  // 同步占位：必须在第一个 await（获取报价）之前设置，双击只发一笔请求。
  submitting.value = true
  tradeError.value = null
  try {
    if (!quote.value) await fetchQuote()
    const q = quote.value
    if (!q) {
      tradeError.value = tradeError.value ?? '暂时拿不到报价，请稍后重试'
      return
    }
    const minOutValue = computeMinOut(q.output_amount, effectiveSlippageBps.value)
    const signature = fxOrderSignature({
      pairId: pid,
      side: side.value,
      amount: amount.value.trim(),
      minOut: minOutValue,
    })
    // 同一逻辑订单复用幂等键；并发在途时 submit 返回 null（不再发请求）。
    const trade = await orderSubmitter.submit(signature, (idempotencyKey) =>
      fxApi.trade(pid, {
        side: side.value,
        amount: amount.value.trim(),
        min_out: minOutValue,
        idempotency_key: idempotencyKey,
      }),
    )
    if (trade === null) return
    orderSubmitter.reset()
    msg.success(
      side.value === 'buy'
        ? `买入成功，预计到账 ${formatFxAmount(q.output_amount)} ${currencyName.value}`
        : `卖出成功，预计到账 ${formatFxAmount(q.output_amount)} 金圆券`,
    )
    amount.value = ''
    quote.value = null
    await refreshAll()
  } catch (e) {
    if (isConflictError(e)) {
      // 409：行情/幂等冲突，刷新 snapshot 后重新报价；参数一致时复用同一幂等键。
      await loadSnapshot().catch(() => {})
      await fetchQuote()
    }
    tradeError.value = mapFxError(e, '成交失败')
  } finally {
    submitting.value = false
  }
}

function setSide(next: FxSide) {
  if (side.value === next) return
  side.value = next
  scheduleQuote()
}

function setChartInterval(next: FxInterval) {
  if (interval.value === next) return
  interval.value = next
  void loadChart()
}

function onPairChange(event: Event) {
  const value = Number((event.target as HTMLSelectElement).value)
  if (Number.isFinite(value) && value > 0 && value !== pairId.value) void selectPair(value)
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isFinite(d.getTime()) ? d.toLocaleString() : '—'
}

watch([amount, side], scheduleQuote)

onMounted(load)

onUnmounted(() => {
  if (quoteTimer) clearTimeout(quoteTimer)
  stream?.disconnect()
  stream = null
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
})
</script>

<template>
  <div class="fx-page">
    <div class="fx-head">
      <div>
        <h1 class="fx-title">幻想外汇</h1>
        <p class="fx-sub">金圆券 ↔ 幻想外币。第一版只有一个货币对，不做杠杆、做空或限价单。</p>
      </div>
      <div class="fx-pair-select">
        <label for="fx-pair">货币对</label>
        <select
          id="fx-pair"
          :value="pairId ?? ''"
          :disabled="pairs.length === 0"
          @change="onPairChange"
        >
          <option v-if="pairs.length === 0" value="">暂无货币对</option>
          <option v-for="p in pairs" :key="p.id" :value="p.id">
            {{ p.currency_name }}（{{ p.currency_code }}）· {{ p.status }}
          </option>
        </select>
        <span class="fx-stream-dot" :class="{ on: streamConnected }" :title="streamConnected ? '实时已连接' : '实时未连接'"></span>
      </div>
    </div>

    <div v-if="loading" class="fx-state">行情加载中…</div>
    <div v-else-if="error" class="fx-state fx-state-error">
      {{ error }}
      <button class="btn-secondary" @click="load">重试</button>
    </div>
    <div v-else-if="!activePair" class="fx-state">
      FX 尚未开市：管理员建立货币对、注资并开市后即可交易。
    </div>

    <template v-else>
      <div v-if="!tradable" class="fx-notice">
        当前货币对状态为「{{ activePair.status }}」，仅可查看行情，不能买卖。
      </div>

      <!-- 行情快照 -->
      <section class="fx-panel fx-quote-panel">
        <div class="fx-quote-main">
          <div class="fx-quote-label">有效买入价（买入外币）</div>
          <div class="fx-quote-value" :class="priceDirection">
            {{ formatFxAmount(snapshot?.buy_price) }}
          </div>
          <div class="fx-quote-unit">金 / 1 {{ currencyName }}</div>
        </div>
        <div class="fx-quote-grid">
          <div class="fx-metric">
            <span>边际汇率</span>
            <strong>{{ formatFxAmount(snapshot?.price) }}</strong>
          </div>
          <div class="fx-metric">
            <span>有效卖出价（卖出外币）</span>
            <strong>{{ formatFxAmount(snapshot?.sell_price) }}</strong>
          </div>
          <div class="fx-metric">
            <span>买卖价差（买入价 − 卖出价）</span>
            <strong>{{ formatFxAmount(snapshot?.spread) }}</strong>
          </div>
          <div class="fx-metric">
            <span>24h 成交量（金圆券口径）</span>
            <strong>{{ formatFxAmount(snapshot?.volume_24h) }}</strong>
          </div>
        </div>
      </section>

      <!-- 图表 -->
      <section class="fx-panel">
        <div class="fx-panel-head">
          <h2>价格走势</h2>
          <div class="fx-intervals">
            <button
              v-for="iv in intervals"
              :key="iv"
              class="fx-interval"
              :class="{ active: interval === iv }"
              @click="setChartInterval(iv)"
            >
              {{ iv }}
            </button>
          </div>
        </div>
        <div class="fx-chart-wrap">
          <div ref="chartRef" class="fx-chart"></div>
          <div v-if="chartLoading" class="fx-chart-overlay">K 线加载中…</div>
          <div v-else-if="candleCount === 0" class="fx-chart-overlay">暂无成交，等待第一笔交易</div>
        </div>
      </section>

      <div class="fx-columns">
        <!-- 兑换表单 -->
        <section class="fx-panel">
          <div class="fx-panel-head">
            <h2>兑换</h2>
            <span class="fx-balance">
              现金/持仓以「我的资产」及下方估值面板为准
            </span>
          </div>

          <div class="fx-side-toggle">
            <button
              class="fx-side"
              :class="{ active: side === 'buy' }"
              @click="setSide('buy')"
            >
              买入 {{ currencyName }}
            </button>
            <button
              class="fx-side"
              :class="{ active: side === 'sell' }"
              @click="setSide('sell')"
            >
              卖出 {{ currencyName }}
            </button>
          </div>

          <label class="fx-field">
            <span>{{ side === 'buy' ? '投入金圆券' : '投入外币' }}（最多 6 位小数）</span>
            <input
              v-model="amount"
              class="fx-input"
              inputmode="decimal"
              placeholder="0.000000"
              :disabled="!tradable || submitting"
            />
          </label>

          <label class="fx-field">
            <span>最大滑点（bps，100 = 1%）</span>
            <input
              v-model.number="slippageBps"
              class="fx-input"
              type="number"
              min="0"
              max="10000"
              step="1"
              :disabled="!tradable || submitting"
            />
          </label>

          <div class="fx-preview">
            <div class="fx-preview-row">
              <span>预计得到</span>
              <strong>{{ quote ? formatFxAmount(quote.output_amount) : '—' }}</strong>
            </div>
            <div class="fx-preview-row">
              <span>手续费</span>
              <strong>{{ quote ? formatFxAmount(quote.fee_amount) : '—' }}</strong>
            </div>
            <div class="fx-preview-row">
              <span>有效成交价（{{ side === 'buy' ? '外币/金' : '金/外币' }}）</span>
              <strong>{{ quote ? formatFxAmount(quote.effective_price) : '—' }}</strong>
            </div>
            <div class="fx-preview-row">
              <span>报价滑点 / 最大滑点</span>
              <strong>
                {{ quoteSlippage === null ? '—' : quoteSlippage.toFixed(2) }} bps /
                {{ effectiveSlippageBps }} bps
              </strong>
            </div>
            <div class="fx-preview-row">
              <span>min-out（服务端最低可接受产出）</span>
              <strong>{{ minOut || '—' }}</strong>
            </div>
          </div>

          <p v-if="tradeError" class="fx-error">{{ tradeError }}</p>
          <p v-else-if="quoting" class="fx-hint">报价更新中…</p>

          <div class="fx-actions">
            <button
              class="btn-primary"
              :disabled="!tradable || submitting || !amountValid"
              @click="submitTrade"
            >
              {{ submitting ? '提交中…' : side === 'buy' ? '买入' : '卖出' }}
            </button>
            <button class="btn-secondary" :disabled="quoting || !amountValid" @click="fetchQuote">
              重新报价
            </button>
          </div>
          <p class="fx-hint">
            成交按钮提交期间会禁用，服务端按幂等键防止重复扣款；价格冲突（409）会刷新行情并重新报价。
          </p>
        </section>

        <!-- 持仓与新闻 -->
        <div class="fx-side-col">
          <section class="fx-panel">
            <h2>外币持仓与估值</h2>
            <div class="fx-preview-row">
              <span>持仓数量（{{ currencyName }}）</span>
              <strong>{{ formatFxAmount(wallet?.foreign_amount ?? 0) }}</strong>
            </div>
            <div class="fx-preview-row">
              <span>持仓成本（金圆券）</span>
              <strong>{{ formatFxAmount(wallet?.cost_basis ?? 0) }}</strong>
            </div>
            <div class="fx-preview-row">
              <span>平均成本（金 / 外币）</span>
              <strong>{{ walletAvgCost === null ? '—' : formatFxAmount(walletAvgCost) }}</strong>
            </div>
            <div class="fx-preview-row">
              <span>持仓市值（MTM）</span>
              <strong>{{ formatFxAmount(summary?.fx_mtm ?? 0) }}</strong>
            </div>
            <div class="fx-preview-row">
              <span>浮动盈亏</span>
              <strong :class="fxPnlPositive ? 'up' : 'down'">
                {{ formatFxAmount(summary?.fx_unrealized_pnl ?? 0) }}
              </strong>
            </div>
            <p class="fx-collateral-note">
              FX 外币资产计入展示净值，<strong>不计入借款抵押价值</strong>，也不能直接用于预测市场、兑换商品或还款。
              有未还借款时不能买入外币，但可以卖出取回金圆券。
            </p>
          </section>

          <section class="fx-panel">
            <h2>市场新闻</h2>
            <ul v-if="newsFeed.length" class="fx-news">
              <li v-for="(n, i) in newsFeed" :key="`${n.published_at}-${i}`">
                <div class="fx-news-title">{{ n.title || '未命名事件' }}</div>
                <div class="fx-news-body">{{ n.body }}</div>
                <div class="fx-news-meta">{{ n.kind || 'macro' }} · {{ formatTime(n.published_at) }}</div>
              </li>
            </ul>
            <p v-else class="fx-hint">当前没有已发布事件。新闻只包含公开标题与定性正文，不含隐藏冲击数值。</p>
          </section>
        </div>
      </div>

      <!-- 成交历史 -->
      <section class="fx-panel">
        <div class="fx-panel-head">
          <h2>我的成交记录</h2>
          <button class="btn-secondary" @click="loadTrades">刷新</button>
        </div>
        <div class="table-wrap">
          <table class="fx-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>方向</th>
                <th>投入</th>
                <th>产出</th>
                <th>手续费</th>
                <th>成交后价格</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="t in trades" :key="t.id">
                <td>{{ formatTime(t.created_at) }}</td>
                <td :class="t.side === 'buy' ? 'up' : 'down'">
                  {{ t.side === 'buy' ? '买外币' : '卖外币' }}
                </td>
                <td>{{ formatFxAmount(t.input_amount) }}</td>
                <td>{{ formatFxAmount(t.output_amount) }}</td>
                <td>{{ formatFxAmount(t.fee_amount) }}</td>
                <td>{{ formatFxAmount(t.post_price) }}</td>
              </tr>
              <tr v-if="trades.length === 0">
                <td colspan="6" class="fx-empty-cell">暂无成交</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="fx-hint">
          只显示当前登录用户的个人成交；公开行情（价格/K 线/新闻）见上方，不包含任何其他用户身份或隐藏事件参数。
        </p>
      </section>
    </template>
  </div>
</template>

<style scoped>
.fx-page {
  padding: 4px;
  max-width: 1200px;
}
.fx-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.fx-title {
  margin: 0 0 4px;
  font-size: 24px;
  font-weight: 800;
}
.fx-sub {
  margin: 0;
  color: #666;
  font-size: 13px;
  max-width: 560px;
}
.fx-pair-select {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #555;
}
.fx-pair-select select {
  border: 2px solid #000;
  background: #fff;
  padding: 6px 8px;
  font-family: inherit;
  font-size: 13px;
}
.fx-stream-dot {
  width: 10px;
  height: 10px;
  border: 1.5px solid #000;
  background: #fff;
  display: inline-block;
}
.fx-stream-dot.on {
  background: #16a34a;
}
.fx-state {
  border: 2px solid #000;
  padding: 24px;
  background: #fff;
  font-weight: 600;
  display: flex;
  gap: 12px;
  align-items: center;
}
.fx-state-error {
  color: var(--color-down, #dc2626);
}
.fx-notice {
  border: 2px solid #b45309;
  background: #fffbeb;
  color: #92400e;
  padding: 10px 12px;
  margin-bottom: 14px;
  font-size: 13px;
  font-weight: 600;
}
.fx-panel {
  border: 2px solid #000;
  background: #fff;
  padding: 16px;
  margin-bottom: 16px;
}
.fx-panel h2 {
  margin: 0 0 10px;
  font-size: 15px;
  font-weight: 800;
}
.fx-panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.fx-quote-panel {
  display: flex;
  gap: 20px;
  flex-wrap: wrap;
  align-items: stretch;
}
.fx-quote-main {
  min-width: 220px;
  border-right: 1px solid #e0e0e0;
  padding-right: 20px;
}
.fx-quote-label {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #777;
}
.fx-quote-value {
  font-size: 40px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.fx-quote-value.up { color: var(--color-up, #16a34a); }
.fx-quote-value.down { color: var(--color-down, #dc2626); }
.fx-quote-unit {
  font-size: 12px;
  color: #777;
}
.fx-quote-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(120px, 1fr));
  gap: 10px 18px;
  flex: 1;
}
.fx-metric {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.fx-metric span {
  font-size: 11px;
  color: #777;
}
.fx-metric strong {
  font-size: 16px;
  font-variant-numeric: tabular-nums;
}
.fx-intervals {
  display: flex;
  gap: 6px;
}
.fx-interval {
  border: 1.5px solid #000;
  background: #fff;
  padding: 2px 10px;
  font-size: 12px;
  cursor: pointer;
}
.fx-interval.active {
  background: #000;
  color: #fff;
}
.fx-chart-wrap {
  position: relative;
  margin-top: 10px;
  height: 360px;
}
.fx-chart {
  width: 100%;
  height: 100%;
}
.fx-chart-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(255, 255, 255, 0.85);
  color: #666;
  font-size: 13px;
}
.fx-columns {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
  gap: 16px;
}
.fx-side-col {
  display: flex;
  flex-direction: column;
}
.fx-side-toggle {
  display: flex;
  border: 2px solid #000;
  margin-bottom: 14px;
}
.fx-side {
  flex: 1;
  padding: 8px;
  background: #fff;
  border: none;
  cursor: pointer;
  font-weight: 700;
  font-size: 13px;
}
.fx-side.active {
  background: #000;
  color: #fff;
}
.fx-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 12px;
  font-size: 12px;
  color: #444;
}
.fx-input {
  border: 2px solid #000;
  padding: 8px 10px;
  font-family: ui-monospace, monospace;
  font-size: 14px;
  background: #fff;
}
.fx-input:disabled {
  background: #f5f5f5;
  color: #888;
}
.fx-preview {
  border: 1.5px solid #000;
  padding: 10px 12px;
  margin: 6px 0 12px;
  background: #fafafa;
}
.fx-preview-row {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: 13px;
  padding: 3px 0;
}
.fx-preview-row span {
  color: #666;
}
.fx-preview-row strong {
  font-variant-numeric: tabular-nums;
}
.fx-preview-row strong.up { color: var(--color-up, #16a34a); }
.fx-preview-row strong.down { color: var(--color-down, #dc2626); }
.fx-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
}
.fx-error {
  color: var(--color-down, #dc2626);
  font-size: 12px;
  font-weight: 600;
  margin: 0 0 8px;
}
.fx-hint {
  font-size: 12px;
  color: #777;
  line-height: 1.5;
  margin: 8px 0 0;
}
.fx-collateral-note {
  margin: 10px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: #444;
  border-left: 3px solid #000;
  padding-left: 8px;
}
.fx-balance {
  font-size: 11px;
  color: #888;
}
.fx-news {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 320px;
  overflow-y: auto;
}
.fx-news li {
  border-bottom: 1px solid #e0e0e0;
  padding: 8px 0;
}
.fx-news-title {
  font-weight: 700;
  font-size: 13px;
}
.fx-news-body {
  font-size: 12px;
  color: #444;
  margin-top: 2px;
  line-height: 1.5;
}
.fx-news-meta {
  font-size: 11px;
  color: #999;
  margin-top: 4px;
}
.table-wrap {
  margin-top: 10px;
  overflow-x: auto;
  border: 2px solid #000;
}
.fx-table {
  width: 100%;
  border-collapse: collapse;
  background: #fff;
}
.fx-table th,
.fx-table td {
  border: 1px solid #ddd;
  padding: 6px 10px;
  text-align: left;
  white-space: nowrap;
  font-size: 12px;
}
.fx-table th {
  background: #000;
  color: #fff;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-size: 11px;
}
.fx-table td.up { color: var(--color-up, #16a34a); }
.fx-table td.down { color: var(--color-down, #dc2626); }
.fx-empty-cell {
  text-align: center;
  color: #888;
}
@media (max-width: 900px) {
  .fx-columns { grid-template-columns: 1fr; }
  .fx-quote-grid { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
  .fx-quote-main { border-right: none; padding-right: 0; }
}
</style>
