<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { useRoute } from 'vue-router'
import {
  computeMaxGoldIn, subtractFxAmounts,
  compareFxAmounts,
  computeMinOut,
  divideFxAmount,
  formatFxAmount,
  formatFxPrice,
  fxApi,
  FxOrderSubmitter,
  fxOrderSignature,
  FxStream,
  isConflictError,
  mapFxError,
  tradeSlippageBps,
} from '@/api/fx'
import { userApi } from '@/api/user'
import type { UserSummary } from '@/types/user'
import { useFxResource } from '@/composables/useFxResource'
import { fxAvailableCash, fxBuyBlockReason, fxGoldPerForeign, fxHoldingValue, fxPairAllowsSide, fxSellAllocation } from '@/utils/fxPresentation'
import type {
  FxPersonalTrade, FxShortPosition, FxShortQuote, FxShortTrade,
  FxChartInterval,
  FxPairPublic,
  FxPriceTick,
  FxPublicFrame,
  FxPublicNews,
  FxQuote,
  FxSide,
  FxSnapshot,
  FxTradePublic,
  FxWalletPublic,
} from '@/types/fx'
import FxCandleChart from '@/components/chart/FxCandleChart.vue'
import MobileTradeDock from '@/components/market/MobileTradeDock.vue'
import { useMobileTradeEntry } from '@/composables/useMobileTradeEntry'

defineOptions({ name: 'FxPage' })

const msg = useMessage()
const route = useRoute()

const loading = ref(true)
const error = ref<string | null>(null)
const pairs = ref<FxPairPublic[]>([])
const pairId = ref<number | null>(null)
const snapshot = ref<FxSnapshot | null>(null)
const tradeResource = useFxResource<FxPersonalTrade[]>()
const { data: trades, loading: tradesLoading, failed: tradesFailed, updatedAt: tradesUpdatedAt } = tradeResource
const walletResource = useFxResource<FxWalletPublic>()
const { data: wallet, loading: walletLoading, failed: walletFailed, updatedAt: walletUpdatedAt } = walletResource
const newsFeed = ref<FxPublicNews[]>([])
const streamConnected = ref(false)
const streamFailed = ref(false)
const marketUpdatedAt = ref('')
const snapshotLoading = ref(false)
const snapshotFailed = ref(false)
let snapshotGeneration = 0
let selectionGeneration = 0
const quoteUpdatedAt = ref(0)
const now = ref(Date.now())
let freshnessTimer: ReturnType<typeof setInterval> | null = null
const quoteExpired = computed(() => !!quoteUpdatedAt.value && now.value - quoteUpdatedAt.value >= 30000)
const streamLabel = computed(() => streamConnected.value ? '实时已连接' : streamFailed.value ? '连接中断 · 正在重连' : '正在连接实时行情')
/** 图表周期性刷新/成交后强制重载用；同时把 SSE 价格转发给图表组件 */
const chartReloadToken = ref(0)
const lastTick = ref<FxPriceTick | null>(null)
const priceDirection = ref<'up' | 'down' | 'neutral'>('neutral')

const intervals: FxChartInterval[] = ['1m', '15m', '1h']
const interval = ref<FxChartInterval>('1m')

const side = ref<FxSide>('buy')
const { tradePanelRef, tradePanelVisible, openTrade } = useMobileTradeEntry(setSide)
const amount = ref('')
const slippageBps = ref<number>(50)
const quote = ref<FxQuote | null>(null)
const quoting = ref(false)
const submitting = ref(false)
const tradeError = ref<string | null>(null)
const orderSubmitter = new FxOrderSubmitter()

const summaryResource = useFxResource<UserSummary>()
const { data: summary, loading: summaryLoading, failed: summaryFailed, updatedAt: summaryUpdatedAt } = summaryResource
const receipt = ref<{ trade: FxTradePublic; currency: string; autoRepay: boolean | null } | null>(null)
const quoteReferencePrice = ref<string | null>(null)

const activePair = computed(() => snapshot.value?.pair.id === pairId.value
  ? snapshot.value.pair : pairs.value.find((p) => p.id === pairId.value) ?? null)
const tradable = computed(() => fxPairAllowsSide(activePair.value, side.value))
const currencyName = computed(() => activePair.value?.currency_name ?? '外币')
/** 交易面板顶部按方向显示对应的有效买卖价 */
const sidePrice = computed(() =>
  side.value === 'buy' ? snapshot.value?.buy_price : snapshot.value?.sell_price,
)

const amountValid = computed(
  () => /^\d+(\.\d{0,6})?$/.test(amount.value.trim()) && compareFxAmounts(amount.value.trim(), '0') === 1,
)
const portions = [25, 50, 75, 100]
const canFillSell = computed(() => side.value === 'sell' && tradable.value && !submitting.value
  && !walletLoading.value && !walletFailed.value && wallet.value?.pair_id === pairId.value
  && compareFxAmounts(wallet.value?.foreign_amount, '0') === 1)

function fillSellPortion(percent: number) {
  if (!canFillSell.value || !wallet.value) return
  amount.value = computeMinOut(wallet.value.foreign_amount, (100 - percent) * 100)
}
const effectiveSlippageBps = computed(() => {
  const v = Number(slippageBps.value)
  if (!Number.isFinite(v)) return 0
  return Math.max(0, Math.min(10000, Math.trunc(v)))
})
const minOut = computed(() =>
  quote.value ? computeMinOut(quote.value.output_amount, effectiveSlippageBps.value) : '',
)
const minOutDisplay = computed(() => (minOut.value ? formatFxAmount(minOut.value) : '—'))
const effectiveGoldPerForeign = computed(() => quote.value ? fxGoldPerForeign(quote.value) : null)
const quotePriceImpact = computed(() => tradeSlippageBps(quoteReferencePrice.value, effectiveGoldPerForeign.value))
const holdingValue = computed(() => fxHoldingValue(wallet.value, snapshot.value?.price))
const fxPnlPositive = computed(() => (compareFxAmounts(holdingValue.value?.pnl, '0') ?? 0) >= 0)
const outputCurrency = computed(() => side.value === 'buy' ? currencyName.value : '金圆券')
const inputCurrency = computed(() => side.value === 'buy' ? '金圆券' : currencyName.value)
const sellAllocation = computed(() => side.value === 'sell' && quote.value && !summaryFailed.value && !summaryLoading.value
  ? fxSellAllocation(quote.value.output_amount, summary.value) : null)
const buyBlockReason = computed(() => {
  if (summaryLoading.value) return '正在刷新账户，请稍候'
  if (summaryFailed.value) return '账户信息刷新失败，请重新加载后买入'
  return fxBuyBlockReason(summary.value, amount.value.trim())
})
const tradeBlockReason = computed(() => {
  if (!tradable.value) return fxPairAllowsSide(activePair.value, 'sell') ? '当前币种只允许卖出，不能买入' : '当前币种未开放交易'
  if (!snapshot.value) return '正在读取行情，请稍候'
  if (side.value === 'buy') return buyBlockReason.value
  if (walletLoading.value) return '正在读取持仓，请稍候'
  if (walletFailed.value || !wallet.value) return '持仓读取失败，请重新加载后卖出'
  if (compareFxAmounts(wallet.value.foreign_amount, '0') !== 1) return '暂无可卖出的持仓'
  if (compareFxAmounts(amount.value.trim(), wallet.value.foreign_amount) === 1) return '卖出数量超过持仓，请减少数量'
  return ''
})
const canFillBuy = computed(() => side.value === 'buy' && tradable.value && !submitting.value
  && !summaryLoading.value && !summaryFailed.value && !!summary.value
  && !fxBuyBlockReason(summary.value, ''))
function fillBuyPortion(percent: number) {
  if (!canFillBuy.value || !summary.value) return
  amount.value = computeMinOut(fxAvailableCash(summary.value), (100 - percent) * 100)
}
const walletAvgCost = computed(() => {
  const w = wallet.value
  if (!w) return null
  return divideFxAmount(w.cost_basis, w.foreign_amount, 12)
})


// Short actions remain separate from spot selling; the server owns all risk gates.
const shortPosition = ref<FxShortPosition | null>(null)
const shortAction = ref<'open' | 'cover'>('open')
const shortAmount = ref('')
const coverAll = ref(false)
const shortQuote = ref<FxShortQuote | null>(null)
const shortError = ref('')
const shortLoading = ref(false)
const shortQuoting = ref(false)
const shortReceipt = ref<FxShortTrade | null>(null)
const shortSubmitter = new FxOrderSubmitter()
let shortGeneration = 0
let shortReadGeneration = 0
const shortValid = computed(() => (shortAction.value === 'cover' && coverAll.value)
  || (/^\d+(\.\d{0,6})?$/.test(shortAmount.value.trim()) && compareFxAmounts(shortAmount.value.trim(), '0') === 1))
const shortExpired = computed(() => !!shortQuote.value && now.value >= Date.parse(shortQuote.value.expires_at))
const shortLimit = computed(() => !shortQuote.value ? '' : shortAction.value === 'open'
  ? computeMinOut(shortQuote.value.output_amount, effectiveSlippageBps.value)
  : computeMaxGoldIn(shortQuote.value.input_amount ?? '', effectiveSlippageBps.value))
const shortCanSubmit = computed(() => shortValid.value && !!shortQuote.value?.executable
  && shortQuote.value.affordable !== false && !!shortLimit.value && !shortExpired.value && !submitting.value && !shortQuoting.value)
const pendingShortInterest = computed(() => {
  const row = shortPosition.value
  if (!row || row.pending_short_debt == null) return null
  const accrued = subtractFxAmounts(row.pending_short_debt, row.principal_foreign)
  return accrued == null ? null : subtractFxAmounts(accrued, row.interest_foreign)
})
const shortPnl = computed(() => shortPosition.value?.reference_cover_cost == null ? null
  : subtractFxAmounts(shortPosition.value.proceeds_basis_gold, shortPosition.value.reference_cover_cost))
async function loadShort() {
  const pid = pairId.value
  if (!pid) return
  const generation = ++shortReadGeneration
  shortLoading.value = true
  try {
    const row = await fxApi.getShort(pid)
    if (pid === pairId.value && generation === shortReadGeneration) shortPosition.value = row
  } catch (e) {
    if (pid === pairId.value && generation === shortReadGeneration) shortError.value = mapFxError(e, '空头读取失败')
  } finally {
    if (generation === shortReadGeneration) shortLoading.value = false
  }
}
async function fetchShortQuote() {
  const pid = pairId.value
  if (!pid || !shortValid.value || shortQuoting.value) return
  const generation = ++shortGeneration
  const action = shortAction.value
  const request = action === 'cover' && coverAll.value ? { action, cover_all: true } : { action, foreign_amount: shortAmount.value.trim() }
  shortQuoting.value = true
  shortQuote.value = null
  shortError.value = ''
  try {
    const q = await fxApi.quoteShort(pid, request)
    if (generation === shortGeneration && pid === pairId.value) {
      if (q.pair_id !== pid || q.action !== action || !!q.cover_all !== !!request.cover_all
        || (request.foreign_amount !== undefined && compareFxAmounts(q.requested_foreign_amount, request.foreign_amount) !== 0)) {
        shortError.value = '报价与当前请求不一致，请重试'
      } else shortQuote.value = q
    }
  } catch (e) {
    if (generation === shortGeneration) shortError.value = mapFxError(e, '空头报价失败')
  } finally {
    if (generation === shortGeneration) shortQuoting.value = false
  }
}
watch([shortAction, shortAmount, coverAll, pairId], () => {
  shortGeneration++
  shortQuote.value = null
  shortQuoting.value = false
  shortError.value = ''
})
async function submitShort() {
  if (submitting.value) return
  if (shortExpired.value) { await fetchShortQuote(); shortError.value = '报价已刷新，请确认新价格后再次提交'; return }
  if (!shortCanSubmit.value || !pairId.value) return
  const pid = pairId.value
  const action = shortAction.value
  const quantity = shortAmount.value.trim()
  const all = action === 'cover' && coverAll.value
  const q = shortQuote.value
  if (!q || !q.executable || q.pair_id !== pid || q.action !== action
    || !!q.cover_all !== all
    || (!all && compareFxAmounts(q.requested_foreign_amount, quantity) !== 0)) {
    shortQuote.value = null
    shortError.value = '订单已变化，请重新报价后确认'
    return
  }
  const limit = shortLimit.value
  const signature = JSON.stringify([action === 'open' ? 'short_open' : 'short_cover', pid, all ? 'cover_all' : quantity, limit])
  submitting.value = true
  try {
    const result = await shortSubmitter.submit(signature, key => action === 'open'
      ? fxApi.openShort(pid, { foreign_amount: quantity, min_gold_out: limit, idempotency_key: key })
      : fxApi.coverShort(pid, { ...(all ? { cover_all: true } : { foreign_amount: quantity }), max_gold_in: limit, idempotency_key: key }))
    if (!result) return
    shortReceipt.value = result
    shortSubmitter.reset()
    shortQuote.value = null
    shortAmount.value = ''
    msg.success(action === 'open' ? '开空已成交，所得已锁定用于回补' : '回补已成交')
    await refreshAll()
    chartReloadToken.value++
  } catch (e) {
    shortQuote.value = null
    shortError.value = mapFxError(e, '空头成交失败，请重新报价')
  } finally { submitting.value = false }
}

// ── 数据加载 ──
async function loadPairs() {
  pairs.value = await fxApi.listPairs()
  const preferred =
    pairs.value.find((p) => p.id === Number(route.query.pair)) ??
    pairs.value.find((p) => p.status === 'trading') ??
    pairs.value.find((p) => p.status !== 'draft') ??
    pairs.value[0]
  if (preferred) pairId.value = preferred.id
}

async function loadSnapshot() {
  const pid = pairId.value
  if (!pid) return
  const request = ++snapshotGeneration
  snapshotLoading.value = true
  try {
    const result = await fxApi.getSnapshot(pid)
    if (pid === pairId.value && request === snapshotGeneration) {
      snapshot.value = result
      snapshotFailed.value = false
      marketUpdatedAt.value = new Date().toLocaleTimeString()
    }
  } catch (e) {
    if (pid === pairId.value && request === snapshotGeneration) snapshotFailed.value = true
    throw e
  } finally {
    if (request === snapshotGeneration) snapshotLoading.value = false
  }
}

async function loadTrades() {
  const pid = pairId.value
  if (pid) await tradeResource.load(() => fxApi.getMyTrades(pid, 50))
}

async function loadWallet() {
  const pid = pairId.value
  if (pid) await walletResource.load(() => fxApi.getWallet(pid))
}

async function loadSummary() {
  await summaryResource.load(() => userApi.getSummary())
}

async function refreshAll() {
  await Promise.allSettled([loadSnapshot(), loadTrades(), loadWallet(), loadSummary(), loadShort()])
}

// ── SSE ──
let stream: FxStream | null = null
function onFrame(frame: FxPublicFrame) {
  if (frame.price === undefined && frame.news === undefined) return
  const current = snapshot.value
  if (current) {
    const prev = Number(current.price)
    const next = frame.price !== undefined ? Number(frame.price) : null
    if (next !== null && Number.isFinite(next) && Number.isFinite(prev)) {
      if (next > prev) priceDirection.value = 'up'
      else if (next < prev) priceDirection.value = 'down'
    }
    snapshot.value = {
      ...current,
      price: frame.price ?? current.price,
      buy_price: frame.buy_price ?? current.buy_price,
      sell_price: frame.sell_price ?? current.sell_price,
      spread: frame.spread ?? current.spread,
      volume_24h: frame.volume ?? current.volume_24h,
    }
  }
  // 价格保持字符串语义转发给图表；图表内部才在适配层转 number
  if (frame.price !== undefined) {
    lastTick.value = { price: frame.price, ts: Date.now() }
    marketUpdatedAt.value = new Date().toLocaleTimeString()
  }
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
      const reconnected = streamFailed.value
      streamConnected.value = true
      streamFailed.value = false
      if (reconnected) {
        void refreshAll()
        chartReloadToken.value += 1
      }
    })
    stream.onError(() => {
      streamFailed.value = true
      streamConnected.value = false
    })
    stream.onFrame(onFrame)
  }
  stream.connect(pid)
}

async function selectPair(id: number) {
  if (submitting.value) return
  const selection = ++selectionGeneration
  snapshotGeneration++
  snapshotFailed.value = false
  streamFailed.value = false
  marketUpdatedAt.value = ''
  stream?.disconnect()
  streamConnected.value = false
  error.value = null
  shortGeneration++
  shortReadGeneration++
  shortReceipt.value = null
  shortQuote.value = null
  shortPosition.value = null
  shortError.value = ''
  pairId.value = id
  amount.value = ''
  quote.value = null
  tradeError.value = null
  newsFeed.value = []
  walletResource.reset()
  tradeResource.reset()
  receipt.value = null
  snapshot.value = null
  lastTick.value = null
  try {
    await Promise.all([loadSnapshot(), loadTrades(), loadWallet(), loadSummary(), loadShort()])
    if (selection === selectionGeneration) connectStream()
  } catch (e) {
    if (selection === selectionGeneration) error.value = mapFxError(e, 'FX 行情加载失败')
  }
}

async function refreshMarket() {
  if (snapshotLoading.value || submitting.value) return
  await refreshAll()
  chartReloadToken.value += 1
  if (!streamConnected.value) {
    stream?.disconnect()
    connectStream()
  }
}

async function load() {
  loading.value = true
  error.value = null
  try {
    await loadPairs()
    if (pairId.value === null) return
    await Promise.all([loadSnapshot(), loadTrades(), loadWallet(), loadSummary(), loadShort()])
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
  quoteGen++
  quoting.value = false
  quote.value = null
  quoteReferencePrice.value = null
  quoteUpdatedAt.value = 0
  tradeError.value = null
  if (quoteTimer) clearTimeout(quoteTimer)
  quoteTimer = setTimeout(() => {
    void fetchQuote()
  }, 350)
}

async function fetchQuote() {
  const pid = pairId.value
  const value = amount.value.trim()
  const requestedSide = side.value
  const referencePrice = snapshot.value?.price ?? null
  if (!pid || !tradable.value || !amountValid.value) {
    quote.value = null
    return
  }
  const gen = ++quoteGen
  quote.value = null
  quoting.value = true
  try {
    const q = await fxApi.getQuote(pid, { side: requestedSide, amount: value })
    if (gen === quoteGen) {
      if (q.pair_id !== pid || q.side !== requestedSide || compareFxAmounts(q.input_amount, value) !== 0) {
        tradeError.value = '报价与当前订单不一致，请重新报价'
        return
      }
      quote.value = q
      quoteUpdatedAt.value = Date.now()
      now.value = Date.now()
      quoteReferencePrice.value = referencePrice
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
  if (quoteExpired.value) {
    tradeError.value = '报价已超过 30 秒，请重新报价后确认'
    return
  }
  if (tradeBlockReason.value) {
    tradeError.value = tradeBlockReason.value
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
    if (quoteTimer) clearTimeout(quoteTimer)
    if (!quote.value) await fetchQuote()
    const q = quote.value
    if (!q || q.pair_id !== pid || q.side !== side.value || compareFxAmounts(q.input_amount, amount.value.trim()) !== 0) {
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
    receipt.value = {
      trade,
      currency: currencyName.value,
      autoRepay: summary.value && !summaryFailed.value
        ? !!summary.value.unified_credit_enabled : null,
    }
    msg.success(trade.side === 'buy'
      ? `买入成功，实际到账 ${formatFxAmount(trade.output_amount)} ${currencyName.value}`
      : `卖出成功，实际成交所得 ${formatFxAmount(trade.output_amount)} 金圆券`)
    amount.value = ''
    quote.value = null
    await refreshAll()
    chartReloadToken.value += 1
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
  if (submitting.value || side.value === next) return
  amount.value = ''
  side.value = next
}

function setChartInterval(next: FxChartInterval) {
  if (interval.value === next) return
  interval.value = next
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

const statusLabel = computed(() => {
  switch (activePair.value?.status) {
    case 'trading':
      return '交易中'
    case 'paused':
      return '暂停'
    case 'closed':
      return '已闭市'
    case 'draft':
      return '草稿'
    default:
      return '未知'
  }
})

watch([amount, side, pairId], scheduleQuote, { flush: 'sync' })

onMounted(() => {
  void load()
  freshnessTimer = setInterval(() => { now.value = Date.now() }, 1000)
})

onUnmounted(() => {
  if (quoteTimer) clearTimeout(quoteTimer)
  quoteGen++
  snapshotGeneration++
  selectionGeneration++
  if (freshnessTimer) clearInterval(freshnessTimer)
  walletResource.reset()
  tradeResource.reset()
  summaryResource.reset()
  stream?.disconnect()
  stream = null
})
</script>

<template>
  <div class="fx-page">
    <!-- ── 顶部行情条：货币对 / 状态 / 当前价 / 关键报价 ── -->
    <header class="fx-topbar">
      <div class="fx-topbar-id">
        <h1 class="fx-title">外汇交易 <small>FX战士</small></h1>
        <div class="fx-pair-row">
          <select
            id="fx-pair"
            class="fx-pair-select"
            :value="pairId ?? ''"
            :disabled="pairs.length === 0 || submitting"
            aria-label="选择货币对"
            @change="onPairChange"
          >
            <option v-if="pairs.length === 0" value="">暂无货币对</option>
            <option v-for="p in pairs" :key="p.id" :value="p.id">
              {{ p.currency_name }}（{{ p.currency_code }}）
            </option>
          </select>
          <span v-if="activePair" class="fx-status" :class="`fx-status-${activePair.status}`">
            {{ activePair.reduce_only && ['trading', 'paused'].includes(activePair.status) ? '只允许卖出' : statusLabel }}
          </span>
          <span class="fx-connection" :class="{ connected: streamConnected }" role="status">
            <span class="fx-stream-dot" :class="{ on: streamConnected }" aria-hidden="true"></span>
            {{ streamLabel }}
          </span>
        </div>
      </div>

      <div class="fx-topbar-price">
        <span class="fx-topbar-label">边际汇率 · 金 / 1 {{ currencyName }}</span>
        <span class="fx-price" :class="priceDirection">{{ formatFxPrice(snapshot?.price) }}</span>
      </div>

      <div class="fx-topbar-stats">
        <div class="fx-stat">
          <span>参考买入价</span>
          <b class="up">{{ formatFxPrice(snapshot?.buy_price) }}</b>
        </div>
        <div class="fx-stat">
          <span>参考卖出价</span>
          <b class="down">{{ formatFxPrice(snapshot?.sell_price) }}</b>
        </div>
        <div class="fx-stat">
          <span>价差</span>
          <b>{{ formatFxPrice(snapshot?.spread) }}</b>
        </div>
        <div class="fx-stat">
          <span>24h 成交额（金圆券）</span>
          <b>{{ formatFxAmount(snapshot?.volume_24h) }}</b>
        </div>
      </div>
    </header>
    <div class="fx-freshness-bar">
      <span>{{ marketUpdatedAt ? `行情最近更新 ${marketUpdatedAt}` : '等待行情数据' }}</span>
      <span v-if="snapshotFailed" class="fx-error" role="alert">行情刷新失败，所示报价可能已过期。</span>
      <span v-else-if="streamFailed" class="fx-hint">实时行情暂不可用，可手动刷新。下单前请重新获取报价。</span>
      <button class="btn-secondary" :disabled="loading || snapshotLoading || submitting || !pairId" @click="refreshMarket">{{ snapshotLoading ? '刷新中…' : '刷新行情与账户' }}</button>
    </div>

    <div v-if="loading" class="fx-state">行情加载中…</div>
    <div v-else-if="error" class="fx-state fx-state-error">
      {{ error }}
      <button class="btn-secondary" @click="load">重试</button>
    </div>
    <div v-else-if="!activePair" class="fx-state">
      外汇交易暂未开放，请稍后再来查看。
    </div>

    <template v-else>
      <div v-if="!tradable" class="fx-notice">
        {{ activePair.reduce_only && ['trading', 'paused'].includes(activePair.status) ? '当前币种仅允许卖出，请切换到卖出。' : `当前货币对状态为「${statusLabel}」，仅可查看行情，不能买卖。` }}
      </div>

      <!-- ── 工作台：K 线主区 + 右侧交易面板（移动端堆叠） ── -->
      <div class="fx-workbench">
        <section class="fx-chart-panel">
          <div class="fx-panel-head">
            <div class="fx-panel-title">
              <h2>K 线</h2>
              <span class="fx-chart-sub">{{ currencyName }} · 金 / {{ currencyName }}</span>
            </div>
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
          <div class="fx-chart-body">
            <FxCandleChart
              v-if="pairId"
              :pair-id="pairId"
              :interval="interval"
              :tick="lastTick"
              :reload-token="chartReloadToken"
              height="100%"
            />
          </div>
        </section>

        <aside class="fx-trade-panel" aria-labelledby="fx-trade-heading">
          <h2 id="fx-trade-heading" ref="tradePanelRef" class="fx-trade-heading" tabindex="-1">交易 {{ currencyName }}</h2>
          <div class="fx-trade-tabs">
            <button
              class="fx-trade-tab"
              :class="{ active: side === 'buy' }"
              :disabled="submitting"
              :aria-pressed="side === 'buy'"
              @click="setSide('buy')"
            >
              买入 {{ currencyName }}
            </button>
            <button
              class="fx-trade-tab"
              :class="{ active: side === 'sell' }"
              :disabled="submitting"
              :aria-pressed="side === 'sell'"
              @click="setSide('sell')"
            >
              卖出 {{ currencyName }}
            </button>
          </div>

          <div class="fx-trade-body">
            <div class="fx-trade-price">
              <span>{{ side === 'buy' ? '参考买入价' : '参考卖出价' }}</span>
              <strong :class="side === 'buy' ? 'up' : 'down'">
                {{ formatFxPrice(sidePrice) }}
              </strong>
            </div>

            <p class="fx-hint">金圆券 / 1 {{ currencyName }}；本笔成交均价见下方报价。</p>

            <div v-if="side === 'buy'" class="fx-sell-holdings">
              <div class="fx-preview-row" aria-live="polite">
                <span>可用金圆券</span>
                <strong>{{ summaryLoading ? '加载中…' : formatFxAmount(fxAvailableCash(summary)) }}</strong>
              </div>
              <div class="fx-sell-shortcuts">
                <button v-for="percent in portions" :key="percent" class="btn-secondary"
                  :disabled="!canFillBuy" @click="fillBuyPortion(percent)">
                  {{ percent === 100 ? '全部' : `${percent}%` }}
                </button>
              </div>
            </div>
            <p v-if="summaryFailed" class="fx-error" role="alert">
              账户刷新失败{{ summary ? '，所示余额和规则为上次快照，暂不能买入' : '' }}。
              <button class="fx-wallet-retry" :disabled="summaryLoading" @click="loadSummary">重新加载账户</button>
            </p>
            <p v-if="summary?.unified_credit_enabled && summary.debt > 0" class="fx-hint">
              当前有借款，买入需通过成交时的风控检查；可用现金不代表可安全投入额度。
              <router-link to="/loan">查看借款与风控</router-link>
            </p>

            <label class="fx-field">
              <span>{{ side === 'buy' ? '投入金圆券' : `卖出数量（${currencyName}）` }}（最多 6 位小数）</span>
              <input
                v-model="amount"
                class="fx-input"
                inputmode="decimal"
                autocomplete="off"
                placeholder="0.000000"
                :disabled="!tradable || submitting"
              />
            </label>

            <div v-if="side === 'sell'" class="fx-sell-holdings">
              <div class="fx-preview-row" aria-live="polite">
                <span>当前持仓（{{ currencyName }}）</span>
                <strong>{{ walletLoading ? '加载中…' : wallet ? formatFxAmount(wallet.foreign_amount, 6) : '暂不可用' }}</strong>
              </div>
              <div class="fx-sell-shortcuts">
                <button v-for="percent in portions" :key="percent" class="btn-secondary"
                  :disabled="!canFillSell" @click="fillSellPortion(percent)">
                  {{ percent === 100 ? '全部' : `${percent}%` }}
                </button>
              </div>
              <p v-if="walletFailed" class="fx-hint">
                持仓读取失败{{ wallet ? '，显示上次快照' : '' }}，<button class="fx-wallet-retry" :disabled="walletLoading" @click="loadWallet">重新加载</button>
              </p>
            </div>

            <label class="fx-field">
              <span>报价变动容忍度</span>
              <select v-model.number="slippageBps" class="fx-input" :disabled="!tradable || submitting">
                <option :value="50">0.5%</option>
                <option :value="100">1%</option>
                <option :value="200">2%</option>
                <option :value="500">5%</option>
              </select>
            </label>
            <p class="fx-hint">相对本次报价，实际到账低于下方最低金额时交易会取消。</p>

            <div class="fx-preview">
              <div class="fx-preview-row">
                <span>{{ side === 'buy' ? '预计到账' : '预计卖出所得' }}</span>
                <strong>{{ quote ? formatFxAmount(quote.output_amount) : '—' }} {{ outputCurrency }}</strong>
              </div>
              <div class="fx-preview-row">
                <span>手续费</span>
                <strong>{{ quote ? formatFxAmount(quote.fee_amount) : '—' }} {{ inputCurrency }}</strong>
              </div>
              <div class="fx-preview-row">
                <span>本笔均价（金圆券 / {{ currencyName }}）</span>
                <strong>{{ formatFxPrice(effectiveGoldPerForeign) }}</strong>
              </div>
              <div class="fx-preview-row">
                <span>价格影响（含手续费）</span>
                <strong>{{ quotePriceImpact === null ? '—' : (quotePriceImpact / 100).toFixed(2) + '%' }}</strong>
              </div>
              <div class="fx-preview-row fx-preview-row--minout">
                <span>{{ side === 'buy' ? '最低到账' : '最低卖出所得' }}</span>
                <strong>{{ minOutDisplay }} {{ outputCurrency }}</strong>
              </div>
            </div>

            <div class="fx-quote-age" role="status">
              <span v-if="quoteExpired" class="fx-error">报价已超过 30 秒，请重新报价后确认。</span>
              <span v-else-if="quoteUpdatedAt">报价更新于 {{ new Date(quoteUpdatedAt).toLocaleTimeString() }} · 30 秒内可提交</span>
              <span v-else>填写金额后获取报价</span>
            </div>
            <details class="fx-quote-explanation">
              <summary>费用与报价说明</summary>
              <p>手续费已计入报价，按投入币种收取。价格影响是本笔均价相对参考边际汇率的差异；报价变动容忍度则决定相对本次报价可接受的最低所得。</p>
            </details>
            <div v-if="side === 'sell' && summary?.unified_credit_enabled" class="fx-preview">
              <div class="fx-preview-row"><span>预计用于还债</span><strong>{{ formatFxAmount(sellAllocation?.repayment) }} 金圆券</strong></div>
              <div class="fx-preview-row"><span>预计现金净增加</span><strong>{{ formatFxAmount(sellAllocation?.cashIncrease) }} 金圆券</strong></div>
              <p class="fx-hint">卖出所得优先偿还借款。这里按最近账户快照估算，实际还款含成交时利息，以账户记录为准。</p>
            </div>
            <p v-if="tradeBlockReason" class="fx-error" role="status">{{ tradeBlockReason }}</p>
            <p v-if="side === 'buy' && summary && summary.debt > 0" class="fx-hint"><router-link to="/loan">查看借款 / 还款 →</router-link></p>

            <div class="fx-submit-state">
              <span v-if="tradeError" class="fx-error">{{ tradeError }}</span>
              <span v-else-if="submitting" class="fx-hint">订单处理中，请稍候…</span>
              <span v-else-if="quoting" class="fx-hint">报价更新中…</span>
              <span v-else class="fx-hint">
                {{ amount && !amountValid ? '请输入正数金额，最多 6 位小数。' : '请核对币种、金额和最低到账后提交。' }}
              </span>
            </div>

            <div class="fx-actions">
              <button
                class="fx-submit"
                :class="side === 'buy' ? 'fx-submit-buy' : 'fx-submit-sell'"
                :disabled="submitting || !amountValid || !!tradeBlockReason || quoting || !quote || quoteExpired"
                @click="submitTrade"
              >
                {{ submitting ? '提交中…' : side === 'buy' ? '买入' : '卖出' }}
              </button>
              <button
                class="btn-secondary"
                :disabled="quoting || submitting || !tradable || !amountValid"
                @click="fetchQuote"
              >
                重新报价
              </button>
            </div>
          </div>
        </aside>
      </div>

      <section v-if="receipt" class="fx-receipt" role="status" aria-live="polite">
        <h2>{{ receipt.trade.side === 'buy' ? '买入已成交' : '卖出已成交' }} · {{ receipt.currency }}</h2>
        <p>实际投入 {{ formatFxAmount(receipt.trade.input_amount) }} {{ receipt.trade.side === 'buy' ? '金圆券' : receipt.currency }}；
          {{ receipt.trade.side === 'buy' ? '实际到账' : '实际成交所得' }} {{ formatFxAmount(receipt.trade.output_amount) }} {{ receipt.trade.side === 'buy' ? receipt.currency : '金圆券' }}。</p>
        <p>手续费 {{ formatFxAmount(receipt.trade.fee_amount) }} {{ receipt.trade.side === 'buy' ? '金圆券' : receipt.currency }}（已含）；
          成交均价 {{ formatFxPrice(fxGoldPerForeign(receipt.trade)) }} 金圆券 / {{ receipt.currency }}。</p>
        <p v-if="receipt.trade.side === 'sell' && receipt.autoRepay !== false" class="fx-hint">如有借款，统一信贷模式下所得会优先还债，成交所得不等于现金净增加。本笔实际还债明细暂未提供，请核对刷新后的余额和负债。</p>
        <p class="fx-hint">{{ formatTime(receipt.trade.created_at) }} · 成交编号 {{ receipt.trade.id }} · <router-link to="/user/portfolio">查看账户资产 →</router-link></p>
      </section>


      <section class="fx-receipt">
        <h2>{{ currencyName }} · 开空 / 回补</h2>
        <p class="fx-hint">借入外币并立即卖出，所得包含在总现金中并锁定用于本仓回补。全部资产共享保证金；汇率上涨可能触发强平。回补不会自动借金圆券。</p>
        <div class="fx-preview-row"><span>总现金 / 可用现金（金圆券）</span><strong>{{ formatFxAmount(summary?.cash) }} / {{ formatFxAmount(fxAvailableCash(summary)) }}</strong></div>
        <p v-if="shortLoading" class="fx-hint">正在读取空头…</p>
        <template v-if="shortPosition">
          <div class="fx-preview-row"><span>外币本金 / 已计利息 / 含待计利息欠币 Q</span><strong>{{ formatFxAmount(shortPosition.principal_foreign) }} / {{ formatFxAmount(shortPosition.interest_foreign) }} / {{ formatFxAmount(shortPosition.pending_short_debt) }} {{ currencyName }}</strong></div>
          <div class="fx-preview-row"><span>待计外币利息</span><strong>{{ formatFxAmount(pendingShortInterest) }} {{ currencyName }}</strong></div>
          <div class="fx-preview-row"><span>锁定所得 S / 剩余开仓所得基准（金）</span><strong>{{ formatFxAmount(shortPosition.restricted_gold) }} / {{ formatFxAmount(shortPosition.proceeds_basis_gold) }}</strong></div>
          <div class="fx-preview-row"><span>全仓参考回补成本 K（金）</span><strong>{{ shortPosition.reference_cover_cost === null ? '未知（不能按零计算）' : formatFxAmount(shortPosition.reference_cover_cost) }}</strong></div>
          <div class="fx-preview-row"><span>参考回补盈亏（基准 − K，含息和手续费）</span><strong>{{ formatFxAmount(shortPnl) }}</strong></div>
          <p class="fx-hint">风险：{{ shortPosition.risk_status }}；全仓参考报价{{ shortPosition.executable ? '可执行' : '不可执行' }} {{ shortPosition.blocked_reason }}</p>
        </template>
        <div class="fx-trade-tabs">
          <button class="fx-trade-tab" :class="{ active: shortAction === 'open' }" :disabled="submitting" @click="shortAction = 'open'">开空 / 加空</button>
          <button class="fx-trade-tab" :class="{ active: shortAction === 'cover' }" :disabled="submitting" @click="shortAction = 'cover'">买回归还</button>
        </div>
        <div class="fx-trade-body">
          <label v-if="shortAction === 'cover'" class="fx-field"><span><input v-model="coverAll" type="checkbox" :disabled="submitting" /> 全部回补（成交时包含新计利息）</span></label>
          <label class="fx-field">{{ shortAction === 'open' ? '借入并卖出的外币数量' : '回补外币数量' }}<input v-model="shortAmount" class="fx-input" inputmode="decimal" :disabled="submitting || (shortAction === 'cover' && coverAll)" placeholder="正数，最多六位小数" /></label>
          <label class="fx-field">最大滑点（bps，100 = 1%）<input v-model="slippageBps" class="fx-input" type="number" min="0" max="10000" :disabled="submitting" /></label>
          <div v-if="shortQuote" class="fx-preview">
            <div class="fx-preview-row"><span>实际 AMM 投入 / 产出</span><strong>{{ formatFxAmount(shortQuote.input_amount) }} {{ shortAction === 'open' ? currencyName : '金圆券' }} / {{ formatFxAmount(shortQuote.output_amount) }} {{ shortAction === 'open' ? '金圆券' : currencyName }}</strong></div>
            <div class="fx-preview-row"><span>手续费（已含）</span><strong>{{ formatFxAmount(shortQuote.fee_amount) }} {{ shortQuote.fee_currency === 'gold' ? '金圆券' : currencyName }}</strong></div>
            <div class="fx-preview-row"><span>锁金变化 / 预计可用现金（金）</span><strong>{{ formatFxAmount(shortQuote.restricted_gold_delta) }} / {{ formatFxAmount(shortQuote.available_cash) }}</strong></div>
            <div class="fx-preview-row"><span>预计风险净值 E / 风险基数 B</span><strong>{{ formatFxAmount(shortQuote.estimated_equity) }} / {{ formatFxAmount(shortQuote.estimated_risk_basis) }}</strong></div>
            <div class="fx-preview-row"><span>{{ shortAction === 'open' ? '最低金所得' : '最高金支出' }}</span><strong>{{ shortLimit }} 金圆券</strong></div>
            <p class="fx-hint">报价有效至 {{ formatTime(shortQuote.expires_at) }}{{ shortExpired ? '（已过期，请重新报价）' : '' }}；成交时服务端重新报价。</p>
            <p v-if="shortQuote.blocked_reason" class="fx-error">订单阻止原因：{{ shortQuote.blocked_reason }}</p>
            <p v-if="shortQuote.affordable === false" class="fx-error">本仓锁金与可用现金不足以支付回补成本。</p>
            <p v-if="shortQuote.risk_blocked_reason" class="fx-hint">组合风险提示：{{ shortQuote.risk_blocked_reason }}；以订单可执行性决定是否允许回补。</p>
          </div>
          <p v-if="shortError" class="fx-error" role="alert">{{ shortError }}</p>
          <div class="fx-actions"><button class="btn-secondary" :disabled="!shortValid || shortQuoting || submitting" @click="fetchShortQuote">{{ shortQuoting ? '报价中…' : '获取 / 刷新报价' }}</button><button class="fx-submit" :disabled="!shortCanSubmit" @click="submitShort">{{ submitting ? '提交中…' : shortAction === 'open' ? '确认开空' : '确认回补' }}</button></div>
          <p v-if="shortReceipt" class="fx-hint">{{ shortReceipt.purpose === 'short_open' ? '开空成交：所得锁定' : '回补成交：外币已归还' }} · 实际投入 {{ formatFxAmount(shortReceipt.input_amount) }} {{ shortReceipt.side === 'buy' ? '金圆券' : currencyName }}，产出 {{ formatFxAmount(shortReceipt.output_amount) }} {{ shortReceipt.side === 'buy' ? currencyName : '金圆券' }}；手续费 {{ formatFxAmount(shortReceipt.fee_amount) }} {{ shortReceipt.side === 'buy' ? '金圆券' : currencyName }} · 成交 {{ shortReceipt.trade_id }}</p>
        </div>
      </section>

      <!-- ── 下方：持仓估值 / 新闻 / 成交记录 ── -->
      <div class="fx-lower">
        <section class="fx-block">
          <h2>{{ currencyName }} · 我的持仓</h2>
          <p v-if="walletLoading" class="fx-hint" role="status">正在刷新持仓{{ wallet ? '，下方为上次快照' : '' }}…</p>
          <p v-if="walletFailed" class="fx-error" role="alert">持仓读取失败{{ wallet ? '，下方为上次快照' : '' }}。
            <button class="fx-wallet-retry" :disabled="walletLoading" @click="loadWallet">重试</button>
          </p>
          <div class="fx-preview-row">
            <span>持仓数量（{{ currencyName }}）</span>
            <strong>{{ formatFxAmount(wallet?.foreign_amount) }}</strong>
          </div>
          <div class="fx-preview-row">
            <span>持仓成本（金圆券）</span>
            <strong>{{ formatFxAmount(wallet?.cost_basis) }}</strong>
          </div>
          <div class="fx-preview-row">
            <span>平均成本（金 / 外币）</span>
            <strong>{{ walletAvgCost === null ? '—' : formatFxPrice(walletAvgCost) }}</strong>
          </div>
          <div class="fx-preview-row">
            <span>本币种账面市值（金圆券）</span>
            <strong>{{ formatFxAmount(holdingValue?.marketValue) }}</strong>
          </div>
          <div class="fx-preview-row">
            <span>本币种浮动盈亏（金圆券）</span>
            <strong :class="fxPnlPositive ? 'up' : 'down'">
              {{ formatFxAmount(holdingValue?.pnl) }}
            </strong>
          </div>
          <p class="fx-hint">{{ walletUpdatedAt ? `持仓读取于 ${walletUpdatedAt}。` : '' }}按当前边际汇率估值，不等于全部卖出的实际所得。</p>
          <div class="fx-account-total">
            <h3>全部 FX · 账户快照</h3>
            <p v-if="summaryLoading" class="fx-hint">正在刷新账户…</p>
            <p v-if="summaryFailed" class="fx-error" role="alert">账户刷新失败{{ summary ? '，显示上次快照' : '' }}。
              <button class="fx-wallet-retry" :disabled="summaryLoading" @click="loadSummary">重试</button>
            </p>
            <div class="fx-preview-row"><span>全部外币市值（金圆券）</span><strong>{{ formatFxAmount(summary?.fx_mtm) }}</strong></div>
            <div class="fx-preview-row"><span>全部外币浮盈亏（金圆券）</span><strong>{{ formatFxAmount(summary?.fx_unrealized_pnl) }}</strong></div>
            <p class="fx-hint">{{ summaryUpdatedAt ? `账户快照读取于 ${summaryUpdatedAt}` : '等待账户数据' }}</p>
          </div>
          <p v-if="!summary" class="fx-collateral-note">借款与抵押规则正在读取，账户加载成功后显示。</p>
          <p v-else-if="summary.unified_credit_enabled" class="fx-collateral-note">
            当前为统一信贷模式：FX 持仓按可执行卖出报价计入清算估值。负债买入需通过风控检查，卖出所得优先偿还借款。
            外币不能直接用于预测市场交易或商品兑换。
            <router-link to="/loan">查看借款与风控规则 →</router-link>
          </p>
          <p v-else class="fx-collateral-note">
            当前为传统信贷模式：FX 计入展示净值，不计入借款抵押价值。有未还借款时不能买入外币，仍可卖出；
            外币不能直接用于预测市场交易、兑换商品或还款。
            <router-link to="/loan">查看借款 / 还款 →</router-link>
          </p>
        </section>

        <section class="fx-block">
          <h2>市场新闻 · {{ currencyName }}</h2>
          <ul v-if="newsFeed.length" class="fx-news">
            <li v-for="(n, i) in newsFeed" :key="`${n.published_at}-${i}`">
              <div class="fx-news-title">{{ n.title || '未命名事件' }}</div>
              <div class="fx-news-body">{{ n.body }}</div>
              <div class="fx-news-meta">{{ formatTime(n.published_at) }}</div>
            </li>
          </ul>
          <p v-else class="fx-hint">本次连接尚未收到新闻。此处显示进入页面后收到的公开事件，不代表此前没有发生事件。</p>
        </section>

        <section class="fx-block fx-trades-block">
          <div class="fx-panel-head">
            <h2>我的成交记录</h2>
            <button class="btn-secondary" :disabled="tradesLoading" @click="loadTrades">{{ tradesLoading ? '刷新中…' : '刷新' }}</button>
          </div>
          <p v-if="tradesFailed" class="fx-error" role="alert">成交记录读取失败{{ trades ? '，下方为上次快照' : '' }}，请点击刷新重试。</p>
          <p v-if="tradesLoading" class="fx-hint" role="status">正在读取成交记录…</p>
          <p v-if="tradesUpdatedAt" class="fx-hint">记录读取于 {{ tradesUpdatedAt }}</p>
          <div class="table-wrap">
            <table class="fx-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>方向</th>
                  <th>投入</th>
                  <th>产出</th>
                  <th>手续费</th>
                  <th>本笔均价（金圆券 / 外币）</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="t in trades ?? []" :key="t.id">
                  <td>{{ formatTime(t.created_at) }}</td>
                  <td :class="t.side === 'buy' ? 'up' : 'down'">
                    {{ t.purpose === 'short_open' ? '开空' : t.purpose === 'short_cover' ? '回补归还' : t.side === 'buy' ? '买外币' : '卖外币' }}
                  </td>
                  <td>{{ formatFxAmount(t.input_amount) }} {{ t.side === 'buy' ? '金圆券' : currencyName }}</td>
                  <td>{{ formatFxAmount(t.output_amount) }} {{ t.side === 'buy' ? currencyName : '金圆券' }}</td>
                  <td>{{ formatFxAmount(t.fee_amount) }} {{ t.side === 'buy' ? '金圆券' : currencyName }}</td>
                  <td>{{ formatFxPrice(fxGoldPerForeign(t)) }}</td>
                </tr>
                <tr v-if="!tradesLoading && !tradesFailed && trades?.length === 0">
                  <td colspan="6" class="fx-empty-cell">暂无成交</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="fx-hint">
            仅显示你在当前币种最近 50 笔成交。手续费已计入交易金额。
          </p>
        </section>
      </div>
    </template>
    <MobileTradeDock
      :visible="!loading && !error && !!activePair && !tradePanelVisible"
      :label="currencyName" :side="side" :disabled="submitting"
      @select="openTrade"
    />
  </div>
</template>

<style scoped>
.fx-title small { font-size: 12px; font-weight: 500; color: #666; margin-left: 6px; }
.fx-connection { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: #555; }
.fx-freshness-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; margin: 0 0 14px; font-size: 12px; color: #555; }
.fx-freshness-bar button { margin-left: auto; }
.fx-trade-heading { padding: 12px 14px 8px; font-size: 15px; font-weight: 800; scroll-margin-top: 80px; }
.fx-trade-heading:focus-visible { outline: 2px solid #000; outline-offset: -2px; }
.fx-quote-age { font-size: 12px; color: #555; margin-bottom: 10px; }
.fx-quote-explanation { font-size: 12px; color: #555; margin-bottom: 12px; }
.fx-quote-explanation summary { cursor: pointer; padding: 6px 0; }
.fx-quote-explanation p { padding-top: 6px; line-height: 1.6; }
@media (max-width: 1279px) { .fx-page { padding-bottom: calc(96px + env(safe-area-inset-bottom, 0px)); } }

.fx-receipt { margin-top: 12px; border: 2px solid #000; padding: 14px; background: #fafafa; font-size: 13px; }
.fx-receipt h2 { font-size: 15px; font-weight: 800; margin-bottom: 8px; }
.fx-account-total { border-top: 1px solid #ddd; margin-top: 14px; padding-top: 10px; }
.fx-account-total h3 { font-size: 13px; font-weight: 700; margin-bottom: 6px; }
.fx-page a { text-decoration: underline; text-underline-offset: 3px; }
.fx-trade-body > .fx-hint, .fx-trade-body > .fx-error { margin-bottom: 10px; }
.fx-trade-tab:disabled { cursor: wait; }

.fx-sell-holdings {
  border: 1px solid #ddd;
  padding: 10px;
  margin-bottom: 16px;
}
.fx-sell-shortcuts {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 6px;
  margin-top: 8px;
}
.fx-sell-shortcuts button {
  padding: 6px 0;
  min-width: 0;
}
.fx-wallet-retry {
  color: inherit;
  text-decoration: underline;
  background: none;
  border: 0;
  cursor: pointer;
}
.fx-page {
  padding: 4px;
  max-width: 1360px;
}
.fx-title {
  margin: 0;
  font-size: 18px;
  font-weight: 800;
  letter-spacing: 0.02em;
}
/* ── 顶部行情条 ── */
.fx-topbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px 28px;
  border: 2px solid #000;
  background: #fff;
  padding: 10px 14px;
  margin-bottom: 12px;
}
.fx-topbar-id {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.fx-pair-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.fx-pair-select {
  border: 2px solid #000;
  background: #fff;
  padding: 5px 8px;
  font-family: inherit;
  font-size: 13px;
  font-weight: 700;
}
.fx-status {
  border: 1.5px solid #000;
  padding: 1px 8px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.fx-status-trading {
  background: #000;
  color: #fff;
}
.fx-status-paused,
.fx-status-closed,
.fx-status-draft {
  background: #fff;
  color: #555;
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
.fx-topbar-price {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.fx-topbar-label {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #777;
}
.fx-price {
  font-size: 34px;
  font-weight: 800;
  line-height: 1.05;
  font-variant-numeric: tabular-nums;
}
.fx-price.up { color: var(--color-up, #16a34a); }
.fx-price.down { color: var(--color-down, #dc2626); }
.fx-topbar-stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(110px, 1fr));
  gap: 6px 22px;
  flex: 1;
  min-width: 0;
}
.fx-stat {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.fx-stat span {
  font-size: 11px;
  color: #777;
}
.fx-stat b {
  font-size: 15px;
  font-variant-numeric: tabular-nums;
}
.fx-stat b.up { color: var(--color-up, #16a34a); }
.fx-stat b.down { color: var(--color-down, #dc2626); }

/* ── 状态 ── */
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
  border: 1px solid #888;
  background: #f5f5f5;
  color: #333;
  padding: 8px 12px;
  margin-bottom: 12px;
  font-size: 13px;
  font-weight: 600;
}

/* ── 工作台 ── */
.fx-workbench {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 340px;
  border: 2px solid #000;
  background: #fff;
}
.fx-chart-panel {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.fx-trade-panel {
  display: flex;
  flex-direction: column;
  border-left: 2px solid #000;
}
.fx-panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 14px;
  border-bottom: 1px solid #e0e0e0;
}
.fx-panel-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.fx-panel-title h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 800;
}
.fx-chart-sub {
  font-size: 12px;
  color: #888;
  font-variant-numeric: tabular-nums;
}
.fx-intervals {
  display: flex;
  gap: 6px;
}
.fx-interval {
  border: 1.5px solid #000;
  background: #fff;
  padding: 2px 12px;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}
.fx-interval.active {
  background: #000;
  color: #fff;
}
.fx-chart-body {
  height: 540px;
  min-height: 0;
}

/* ── 交易面板 ── */
.fx-trade-tabs {
  display: flex;
  border-bottom: 2px solid #000;
}
.fx-trade-tab {
  flex: 1;
  padding: 10px 6px;
  background: #fff;
  border: none;
  cursor: pointer;
  font-weight: 800;
  font-size: 13px;
  color: #555;
}
.fx-trade-tab + .fx-trade-tab {
  border-left: 1px solid #000;
}
.fx-trade-tab.active {
  background: #000;
  color: #fff;
}
.fx-trade-body {
  padding: 12px 14px 14px;
  display: flex;
  flex-direction: column;
}
.fx-trade-price {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
  border-bottom: 1px solid #e0e0e0;
  padding-bottom: 8px;
  margin-bottom: 10px;
}
.fx-trade-price span {
  font-size: 12px;
  color: #666;
}
.fx-trade-price strong {
  font-size: 20px;
  font-variant-numeric: tabular-nums;
}
.fx-trade-price strong.up { color: var(--color-up, #16a34a); }
.fx-trade-price strong.down { color: var(--color-down, #dc2626); }
.fx-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 10px;
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
  border: 1px solid #ddd;
  padding: 12px;
  margin: 2px 0 10px;
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
  text-align: right;
}
.fx-preview-row strong.up { color: var(--color-up, #16a34a); }
.fx-preview-row strong.down { color: var(--color-down, #dc2626); }
.fx-preview-row--minout strong {
  font-family: ui-monospace, monospace;
  font-size: 12px;
}
.fx-submit-state {
  min-height: 34px;
  display: flex;
  align-items: flex-start;
}
.fx-error {
  color: var(--color-down, #dc2626);
  font-size: 12px;
  font-weight: 600;
  margin: 0;
}
.fx-hint {
  font-size: 12px;
  color: #777;
  line-height: 1.5;
  margin: 0;
}
.fx-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
}
.fx-submit {
  flex: 1;
  min-width: 120px;
  border: 2px solid #000;
  background: #000;
  color: #fff;
  padding: 10px 16px;
  font-size: 14px;
  font-weight: 800;
  cursor: pointer;
}
.fx-submit:disabled {
  background: #999;
  border-color: #999;
  cursor: not-allowed;
}
.fx-submit-sell {
  background: #fff;
  color: #000;
}
.fx-submit-sell:disabled {
  background: #f0f0f0;
  color: #999;
  border-color: #999;
}
.fx-submit:not(:disabled):hover {
  transform: translate(-1px, -1px);
  box-shadow: 3px 3px 0 #000;
}

/* ── 下方区块 ── */
.fx-lower {
  margin-top: 12px;
  border: 2px solid #000;
  background: #fff;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
}
.fx-block {
  padding: 14px;
  min-width: 0;
}
.fx-block:nth-child(2) {
  border-left: 2px solid #000;
}
.fx-block h2 {
  margin: 0 0 10px;
  font-size: 15px;
  font-weight: 800;
}
.fx-trades-block {
  grid-column: 1 / -1;
  border-top: 2px solid #000;
}
.fx-trades-block .fx-panel-head {
  padding: 0 0 8px;
  border-bottom: none;
}
.fx-collateral-note {
  margin: 10px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: #444;
  border-left: 3px solid #000;
  padding-left: 8px;
}
.fx-news {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 300px;
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
  color: #666;
  margin-top: 4px;
}
.table-wrap {
  margin-top: 8px;
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
  font-variant-numeric: tabular-nums;
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

@media (max-width: 1024px) {
  .fx-workbench {
    grid-template-columns: 1fr;
  }
  .fx-trade-panel {
    border-left: none;
    border-top: 2px solid #000;
  }
  .fx-chart-body {
    height: 400px;
  }
  .fx-lower {
    grid-template-columns: 1fr;
  }
  .fx-block:nth-child(2) {
    border-left: none;
    border-top: 2px solid #000;
  }
  .fx-topbar-stats {
    grid-template-columns: repeat(2, minmax(110px, 1fr));
  }
}
@media (max-width: 560px) {
  .fx-price {
    font-size: 26px;
  }
  .fx-chart-body { height: 280px; }
  .fx-interval, .fx-sell-shortcuts button, .fx-actions button { min-height: 44px; }
  .fx-input { min-height: 44px; font-size: 16px; }
  .fx-trade-tabs button { min-height: 48px; }
  .fx-topbar-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); width: 100%; flex-basis: 100%; }
  .fx-pair-select { max-width: 100%; }
  .fx-pair-row { min-width: 0; }
  .fx-preview-row { font-size: 13px; flex-wrap: wrap; }
  .fx-preview-row strong { overflow-wrap: anywhere; }
  .fx-freshness-bar button { min-height: 40px; }

  .fx-submit {
    flex-basis: 100%;
  }
}
</style>
