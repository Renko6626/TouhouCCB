<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { useRoute } from 'vue-router'
import {
  FxPendingShortOrder,
  addFxAmounts,
  computeMaxGoldIn,
  subtractFxAmounts,
  compareFxAmounts,
  computeMinOut,
  divideFxAmount,
  formatFxAmount,
  formatFxPrice,
  multiplyFxAmount,
  fxApi,
  FxStream,
  mapFxError,
} from '@/api/fx'
import type { FxPendingShortRequest } from '@/api/fx'
import { useTradingSessionStore } from '@/stores/tradingSession'
import { accrueTradingDebt } from '@/utils/tradingRisk'
import { quoteFx, simulateFxShortCover, valueFxPostTrade } from '@/utils/fxSimulation'
import { safeEconomicVersion } from '@/api/fxNetTrading'
import { useAuthStore } from '@/stores/auth'
import type { UserSummary } from '@/types/user'
import { useFxResource } from '@/composables/useFxResource'
import { FxPendingBorrowBuyOrder } from '@/api/fxBorrowBuy'
import type { FxBorrowBuyTrade, FxNetQuote, FxNetTrade, FxShortQuote } from '@/types/fx'
import { FxPendingNetOrder, fxNetApi } from '@/api/fxNetTrading'
import FxTriggerOrders from '@/components/market/FxTriggerOrders.vue'
import FxOrderSize from '@/components/market/FxOrderSize.vue'
import FxBorrowConfirmation from '@/components/market/FxBorrowConfirmation.vue'
import { useTradingPreferencesStore } from '@/stores/tradingPreferences'
import { useFxBorrowConfirmation } from '@/composables/useFxBorrowConfirmation'
import { useFxReferenceAmount } from '@/composables/useFxReferenceAmount'
import FxExecutionPrice from '@/components/market/FxExecutionPrice.vue'
import FxClosePnl from '@/components/market/FxClosePnl.vue'
import FxPositionCard from '@/components/market/FxPositionCard.vue'
import FxSubmitSummary from '@/components/market/FxSubmitSummary.vue'
import {
  fxAvailableCash,
  fxNetActionLabel,
  fxNetBorrowAction,
  fxGroupPersonalTrades,
  fxNetFundingPreview,
  fxNetAllowsGrowth,
  fxGoldPerForeign,
  fxHoldingValue,
  fxPairAllowsSide,
  fxOrderPrincipal,
  fxMarginPercent,
  fxShortGoldPerForeign,
  fxSpotClosePreview,
  fxShortClosePreview,
  fxHoldingAveragePrice,
} from '@/utils/fxPresentation'
import { resolveCreditRiskStatus } from '@/utils/creditRiskStatus'
import type {
  FxTriggerOrder,
  FxPersonalTrade,
  FxShortPosition,
  FxShortTrade,
  FxShortQuoteRequest,
  FxChartInterval,
  FxPairPublic,
  FxPublicEnvelope,
  FxSide,
  FxSnapshot,
  FxWalletPublic,
  FxHoldingPriceLine,
} from '@/types/fx'
import FxCandleChart from '@/components/chart/FxCandleChart.vue'
import MobileTradeDock from '@/components/market/MobileTradeDock.vue'
import { useMobileTradeEntry } from '@/composables/useMobileTradeEntry'

defineOptions({ name: 'FxPage' })

// Display precision only; economic values and submitted amounts remain unchanged.
const displayAmount = (value: Parameters<typeof formatFxAmount>[0], digits = 2) => formatFxAmount(value, digits)

const msg = useMessage()
const route = useRoute()
const authStore = useAuthStore()
const tradingSession = useTradingSessionStore()
let tradingLease: number | undefined
const tradingPreferences = useTradingPreferencesStore()
const borrowConfirmation = useFxBorrowConfirmation(
  () => JSON.stringify([authStore.sessionGeneration, authStore.user?.id, pairId.value,
    tradingSession.account?.economic_version, tradingSession.rules?.rule_version, tradingSession.ready,
    tradeMode.value, side.value, amount.value, shortAction.value, shortAmount.value, coverAll.value,
    closeOnly.value, slippageBps.value, submitting.value, pendingRisk.value]),
  () => tradingPreferences.confirmBorrow,
  () => { if (!tradingPreferences.setConfirmBorrow(false)) msg.warning('本地设置未能保存，之后仍会显示借款确认。') },
)
const borrowConfirmationPending = borrowConfirmation.pending
watch(borrowConfirmation.error, (error) => { if (error) msg.warning(error) })
const currentPricing = computed(() => {
  const group = tradingSession.groups.get(`fx:${pairId.value}`)
  return group?.product === 'fx' ? group : null
})

const loading = ref(true)
const error = ref<string | null>(null)
const pairs = ref<FxPairPublic[]>([])
const pairId = ref<number | null>(null)
const snapshot = ref<FxSnapshot | null>(null)
const tradeResource = useFxResource<FxPersonalTrade[]>()
const {
  data: trades,
  loading: tradesLoading,
  failed: tradesFailed,
  updatedAt: tradesUpdatedAt,
} = tradeResource
const walletResource = useFxResource<FxWalletPublic>()
const {
  data: wallet,
  loading: walletLoading,
  failed: walletFailed,
} = walletResource
const streamConnected = ref(false)
const streamFailed = ref(false)
const marketUpdatedAt = ref('')
const snapshotLoading = ref(false)
const snapshotFailed = ref(false)
let snapshotGeneration = 0
let selectionGeneration = 0
const now = ref(Date.now())
let freshnessTimer: ReturnType<typeof setInterval> | null = null
const quoteExpired = computed(
  () => !!quote.value && now.value >= Date.parse(quote.value.expires_at),
)
const streamLabel = computed(() =>
  streamConnected.value ? '实时行情' : streamFailed.value ? '连接中断，正在重连' : '连接行情中',
)
/** 图表成交后补尾段，保留主线封存历史与真实成交信封。 */
const chartReloadToken = ref(0)
const chartEnvelope = ref<FxPublicEnvelope | null>(null)
const priceDirection = ref<'up' | 'down' | 'neutral'>('neutral')

const intervals: FxChartInterval[] = ['1m', '15m', '1h']
const interval = ref<FxChartInterval>('1m')

const tradeMode = ref<'spot' | 'short'>(route.query.action === 'cover' ? 'short' : 'spot')
const side = ref<FxSide>(route.query.action === 'open' ? 'sell' : 'buy')
const closeOnly = ref(false)
const allowBorrow = computed(() => side.value === 'buy' && compareFxAmounts(fundingAmounts.value?.shortfall, '0') === 1)
const { tradePanelRef, tradePanelVisible } = useMobileTradeEntry(setSide)
const workbenchRef = ref<HTMLElement | null>(null)
const workbenchHeight = ref<string>()
let workbenchObserver: ResizeObserver | null = null
function resizeWorkbench() {
  const el = workbenchRef.value
  if (!el || window.innerWidth <= 950) {
    workbenchHeight.value = undefined
    return
  }
  // Measure the page offset, so scrolling cannot silently resize the order area.
  const top =
    el.getBoundingClientRect().top + window.scrollY + (el.closest('.app-main')?.scrollTop ?? 0)
  workbenchHeight.value = `${Math.max(440, Math.floor(window.innerHeight - top - 16))}px`
}
watch(
  workbenchRef,
  (el) => {
    workbenchObserver?.disconnect()
    if (!el) return
    workbenchObserver = new ResizeObserver(resizeWorkbench)
    workbenchObserver.observe(el.closest('.fx-page') ?? el)
    resizeWorkbench()
  },
  { flush: 'post' },
)
const amount = ref('')
const slippageBps = ref<number>(500)
const quote = ref<FxNetQuote | null>(null)
const quoting = ref(false)
const submitting = ref(false)
const tradeError = ref<string | null>(null)

const summaryResource = useFxResource<UserSummary>()
const {
  data: summary,
  loading: summaryLoading,
  failed: summaryFailed,
  updatedAt: summaryUpdatedAt,
} = summaryResource
const receipt = ref<{ trade: FxNetTrade; currency: string } | null>(null)
const quoteReferencePrice = ref<string | null>(null)

const activePair = computed(() =>
  snapshot.value?.pair.id === pairId.value
    ? snapshot.value.pair
    : (pairs.value.find((p) => p.id === pairId.value) ?? null),
)
const tradable = computed(() => fxPairAllowsSide(activePair.value, 'sell'))
const currencyName = computed(() => activePair.value?.currency_name ?? '外币')
/** 当前交易池储备对应的参考汇率。 */
const localReferencePrice = computed(() => divideFxAmount(currentPricing.value?.gold_reserve, currentPricing.value?.foreign_reserve, 12))
const sellSizeUnit = ref<'gold' | 'foreign'>('gold')
const { goldAmount: sellGoldAmount, updateGoldAmount: updateSellGoldAmount } = useFxReferenceAmount(amount, localReferencePrice)
const amountValid = computed(
  () =>
    /^\d+(\.\d{0,6})?$/.test(amount.value.trim()) &&
    compareFxAmounts(amount.value.trim(), '0') === 1,
)
const portions = [25, 50, 75, 100]
const hasSpotHolding = computed(
  () =>
    wallet.value?.pair_id === pairId.value &&
    compareFxAmounts(wallet.value?.foreign_amount, '0') === 1,
)
const canSellSpotHolding = computed(
  () =>
    fxPairAllowsSide(activePair.value, 'sell') &&
    !submitting.value &&
    !pendingRisk.value &&
    !walletLoading.value &&
    !walletFailed.value &&
    wallet.value?.pair_id === pairId.value &&
    compareFxAmounts(wallet.value?.foreign_amount, '0') === 1,
)
const canFillSell = computed(() => side.value === 'sell' && canSellSpotHolding.value)

function fillSellPortion(percent: number) {
  if (!canFillSell.value || !wallet.value) return
  closeOnly.value = true
  amount.value = computeMinOut(wallet.value.foreign_amount, (100 - percent) * 100)
}

async function prepareSpotSellAll(percent = 100) {
  if (!canSellSpotHolding.value) return
  setSide('sell')
  fillSellPortion(percent)
  await focusTradePanel()
  // The existing amount/side watcher obtains a quote; submission stays explicit.
}
const effectiveSlippageBps = computed(() => {
  const v = Number(slippageBps.value)
  if (!Number.isFinite(v)) return 0
  return Math.max(0, Math.min(10000, Math.trunc(v)))
})
const minOut = computed(() =>
  quote.value ? computeMinOut(quote.value.output_amount, effectiveSlippageBps.value) : '',
)
const minOutDisplay = computed(() => (minOut.value ? displayAmount(minOut.value) : '—'))
const effectiveGoldPerForeign = computed(() => (quote.value ? fxGoldPerForeign(quote.value) : null))
const holdingValue = computed(() => fxHoldingValue(wallet.value, localReferencePrice.value))
const shortHoldingPnl = computed(() =>
  shortPosition.value?.reference_cover_cost == null
    ? null
    : subtractFxAmounts(
        shortPosition.value.proceeds_basis_gold,
        shortPosition.value.reference_cover_cost,
      ),
)
const spotHoldingClose = computed(() => {
  if (!currentPricing.value || compareFxAmounts(wallet.value?.foreign_amount, '0') !== 1) return null
  try { return quoteFx('sell', wallet.value!.foreign_amount, currentPricing.value) }
  catch { return null }
})
const spotHoldingClosePnl = computed(() => {
  const close = spotHoldingClose.value, basis = wallet.value?.cost_basis
  return close && basis != null ? subtractFxAmounts(close.output_amount, basis) : null
})
const newShortLeg = computed(() => quote.value?.legs.find(leg => leg.purpose === 'short_open'))
const crossesPosition = computed(() => (quote.value?.legs.length ?? 0) > 1)
const spotOutputLabel = computed(() => side.value === 'sell' ? (newShortLeg.value && !crossesPosition.value ? '预计所得（锁定）' : '预计所得')
  : crossesPosition.value ? '预计买回（含回补）' : quote.value?.legs[0]?.purpose === 'short_cover' ? '预计回补' : '预计新增持仓')
const shortNotice = computed(() => {
  if (shortError.value || shortQuoteError.value) return shortError.value || shortQuoteError.value
  const q = shortQuote.value
  if (q?.blocked_reason) return shortBlockMessage(q.blocked_reason)
  if (q?.affordable === false) return '回补资金不足，请减少数量或补充现金。'
  if (summaryFailed.value) return '账户更新失败，请刷新账户。'
  return ''
})
const outputCurrency = computed(() => (side.value === 'buy' ? currencyName.value : '金圆券'))
const inputCurrency = computed(() => (side.value === 'buy' ? '金圆券' : currencyName.value))
const spotClosePreview = computed(() =>
  !quoting.value &&
  !quoteExpired.value &&
  !walletLoading.value &&
  !walletFailed.value &&
  wallet.value?.pair_id === pairId.value &&
  quote.value?.pair_id === pairId.value
    ? fxSpotClosePreview(quote.value.legs.find(leg => leg.purpose === 'spot' && leg.side === 'sell')
        ? { ...quote.value, ...quote.value.legs.find(leg => leg.purpose === 'spot' && leg.side === 'sell')! } : quote.value, wallet.value)
    : null,
)
const spotCloseUnavailable = computed(() => {
  if (quoteExpired.value) return '报价已过期，重新报价后显示盈亏。'
  if (walletLoading.value) return '正在更新持仓成本…'
  if (walletFailed.value) return '持仓成本更新失败，请重新加载。'
  if (!quote.value || quoting.value) return '等待本笔卖出报价。'
  return '持仓成本或卖出数量暂不可确认。'
})
const tradeBlockReason = computed(() => {
  if (!tradable.value) return '当前币种未开放交易'
  if (summaryLoading.value || walletLoading.value || shortLoading.value)
    return '正在更新账户，请稍候'
  if (summaryFailed.value || walletFailed.value || shortFailed.value)
    return '账户更新失败，请重新加载'
  if (netStorageBlocked.value) return '无法安全保存订单，请核对账户后重试'
  if (quote.value && !quote.value.executable)
    return mapFxError({ data: { detail: quote.value.blocked_reason } }, '本笔暂不能成交')
  if (closeOnly.value && quote.value && compareFxAmounts(quote.value.growth_amount, '0') === 1)
    return '快捷平仓只卖出已有持仓，请重新选择数量'
  return ''
})
const holdingAuthGeneration = ref(0)
const walletHoldingGeneration = ref(-1)
const shortHoldingGeneration = ref(-1)
const waitingOrders = ref<readonly FxTriggerOrder[]>([])
const triggerOrders = computed(() => waitingOrders.value.filter(order => order.status === 'waiting' && order.pair_id === pairId.value))
const holdingPriceLines = computed<FxHoldingPriceLine[]>(() => {
  const lines: FxHoldingPriceLine[] = []
  if (!authStore.user?.id) return lines
  if (
    walletHoldingGeneration.value === holdingAuthGeneration.value &&
    !walletLoading.value &&
    !walletFailed.value &&
    wallet.value?.pair_id === pairId.value
  ) {
    const price = fxHoldingAveragePrice(wallet.value.cost_basis, wallet.value.foreign_amount)
    if (price) lines.push({ side: 'long', price })
  }
  if (
    shortHoldingGeneration.value === holdingAuthGeneration.value &&
    !shortLoading.value &&
    !shortFailed.value &&
    shortPosition.value?.pair_id === pairId.value
  ) {
    const price = fxHoldingAveragePrice(
      shortPosition.value.proceeds_basis_gold,
      shortPosition.value.pending_short_debt,
    )
    if (price) lines.push({ side: 'short', price })
  }
  return lines
})

// Short actions remain separate from spot selling; the server owns all risk gates.
const shortPosition = ref<FxShortPosition | null>(null)
const shortAction = ref<'open' | 'cover'>(route.query.action === 'cover' ? 'cover' : 'open')
const shortAmount = ref('')
const shortSizeUnit = ref<'gold' | 'foreign'>('gold')
const shortGoldAmount = ref('')
const coverAll = ref(route.query.action === 'cover')
const shortError = ref('')
const shortLoading = ref(false)
const shortFailed = ref(false)
const shortReceipt = ref<FxShortTrade | null>(null)
const shortReceiptCurrency = computed(
  () =>
    pairs.value.find((p) => p.id === shortReceipt.value?.pair_id)?.currency_name ??
    `外币（货币对 ${shortReceipt.value?.pair_id}）`,
)
const shortSubmitter = new FxPendingShortOrder(authStore.user?.id ?? null)
const pendingShort = ref<FxPendingShortRequest | null>(shortSubmitter.pending)
const shortUnresolved = ref(shortSubmitter.hasUnresolved)
let shortAccountGeneration = 0
const borrowSubmitter = new FxPendingBorrowBuyOrder(authStore.user?.id ?? null)
const borrowPending = ref(borrowSubmitter.pending)
const borrowStorageBlocked = ref(borrowSubmitter.storageError)
const borrowUnresolved = ref(borrowSubmitter.hasUnresolved)
const netSubmitter = new FxPendingNetOrder(authStore.user?.id ?? null)
const netPending = ref(netSubmitter.pending)
const netStorageBlocked = ref(netSubmitter.storageError)
const netUnresolved = ref(netSubmitter.hasUnresolved)
const pendingRisk = computed(
  () => shortUnresolved.value || borrowUnresolved.value || netUnresolved.value,
)
const pendingRecordsReviewed = ref(false)
function clearReviewedOrders() {
  if (!pendingRecordsReviewed.value || submitting.value) return
  const orders = [netSubmitter, borrowSubmitter, shortSubmitter]
  if (orders.some(order => order.busy)) return
  let cleared = true
  for (const order of orders) {
    if (order.hasUnresolved && !order.clearAfterReview()) cleared = false
  }
  syncPendingOrders()
  pendingRecordsReviewed.value = false
  if (!cleared) {
    msg.warning('本地记录仍无法清除，请恢复浏览器存储并刷新页面后重试。')
    return
  }
  tradeError.value = null
  borrowError.value = ''
  shortError.value = ''
  msg.success('本地待确认记录已清除，请更新账户后再下单。')
  void refreshAll()
}
const borrowError = ref('')
const borrowReceipt = ref<{ trade: FxBorrowBuyTrade; currency: string } | null>(null)
let borrowGeneration = 0
if (pendingShort.value) tradeMode.value = 'short'
const shortStorageBlocked = ref(shortSubmitter.storageError || !authStore.user?.id)
watch(
  () => authStore.user?.id,
  (id) => {
    pendingRecordsReviewed.value = false
    holdingAuthGeneration.value++
    shortAccountGeneration++
    shortLoading.value = false
    shortFailed.value = false
    shortError.value = ''
    submitting.value = false
    summaryResource.reset()
    walletResource.reset()
    tradeResource.reset()
    shortPosition.value = null
    shortReceipt.value = null
    netSubmitter.setUser(id ?? null)
    receipt.value = null
    quote.value = null
    if (netSubmitter.busy) submitting.value = false
    borrowGeneration++
    if (borrowSubmitter.busy) submitting.value = false
    borrowSubmitter.setUser(id ?? null)
    borrowReceipt.value = null
    borrowError.value = ''
    amount.value = ''
    shortSubmitter.setUser(id ?? null)
    syncPendingOrders()
    if (id && pairId.value) {
      void nextTick(() => refreshAll())
    }
  },
  { immediate: true },
)
const shortValid = computed(
  () =>
    (shortAction.value === 'cover' && coverAll.value) ||
    (/^\d+(\.\d{0,6})?$/.test(shortAmount.value.trim()) &&
      compareFxAmounts(shortAmount.value.trim(), '0') === 1),
)
const shortQuoteOrder = computed<{ pairId: number; body: FxShortQuoteRequest } | null>(() => {
  if (
    !authStore.user?.id ||
    loading.value ||
    tradeMode.value !== 'short' ||
    !pairId.value ||
    !shortValid.value ||
    submitting.value ||
    pendingRisk.value ||
    shortStorageBlocked.value
  )
    return null
  const action = shortAction.value
  return {
    pairId: pairId.value,
    body:
      action === 'cover' && coverAll.value
        ? { action, cover_all: true }
        : { action, foreign_amount: shortAmount.value.trim() },
  }
})
const shortQuote = ref<FxShortQuote | null>(null)
const shortQuoting = ref(false)
const shortQuoteError = ref('')
const shortReferencePrice = computed(() => localReferencePrice.value)
const localShortPreview = computed(() => {
  const order = shortQuoteOrder.value, pricing = currentPricing.value
  if (!order || !pricing) return null
  try {
    const foreign = order.body.cover_all ? shortPosition.value?.pending_short_debt : order.body.foreign_amount
    if (!foreign || compareFxAmounts(foreign, '0') !== 1) return null
    const account = tradingSession.account, rules = tradingSession.rules
    if (!account || !rules) return null
    const preview = order.body.action === 'cover'
      ? simulateFxShortCover(account, pricing, rules, order.body.cover_all ? null : foreign, tradingSession.now)
      : tradingSession.previewFx({ side: 'sell', amount: foreign, borrow_amount: '0', allow_growth: true }, order.pairId)
    if (preview && order.body.action === 'cover') {
      preview.account_preview = valueFxPostTrade(account, preview, [...tradingSession.groups.values()], rules,
        tradingSession.now, tradingSession.accountPreview?.valuation_cache)
      if (!tradingSession.ready && preview.condition !== 'rejected') {
        preview.condition = 'unknown'; preview.unchecked_reasons.push('trading_session_not_ready')
      }
    }
    const budget = order.body.action === 'cover' ? preview?.legs[0]?.input_amount ?? '' : foreign
    return preview ? { preview, budget, foreign, order } : null
  } catch { return null }
})
watch(localShortPreview, (local) => {
  shortQuoteError.value = ''
  if (!local) { shortQuote.value = null; return }
  const { preview: p, order, foreign, budget } = local
  const account = p.account_preview
  const risk = !tradingSession.ready || account?.risk_status === 'unknown' ? 'blocked'
    : account?.maintenance_condition === 'rejected' ? 'danger' : account?.initial_condition === 'rejected' ? 'warning' : 'healthy'
  const wrongEntry = order.body.action === 'open' && hasSpotHolding.value
  const executable = !wrongEntry && (p.condition === 'satisfied' || (order.body.action === 'cover' && p.condition === 'unknown' && p.rejected_reasons.length === 0 && compareFxAmounts(p.growth_amount, '0') === 0))
  shortQuote.value = {
    pair_id: order.pairId, action: order.body.action, purpose: order.body.action === 'open' ? 'short_open' : 'short_cover',
    requested_foreign_amount: order.body.foreign_amount ?? null, cover_all: !!order.body.cover_all,
    actual_foreign_amount: foreign, input_amount: budget, output_amount: p.output_amount,
    fee_amount: p.fee_amount, fee_currency: order.body.action === 'open' ? currencyName.value : '金圆券',
    restricted_gold_delta: p.restricted_gold_delta, available_cash: account?.available_cash ?? null,
    affordable: p.rejected_reasons.length === 0, estimated_equity: account?.liquidation_equity ?? null,
    estimated_risk_basis: account?.risk_basis ?? null, margin_status: risk, risk_status: risk,
    risk_blocked_reason: account?.unknown_reasons.join('、') || null,
    executable,
    blocked_reason: executable ? null : wrongEntry ? 'spot_position_exists' : p.rejected_reasons[0] ?? (p.condition === 'unknown' ? p.unchecked_reasons[0] ?? '估值未知' : null),
    expires_at: new Date(Date.now() + 30000).toISOString(),
  }
}, { flush: 'sync' })
async function fetchShortQuote() {
  await tradingSession.checkVersions()
  // Explicit reduction fallback keeps the original authoritative cover entry available.
  if (localShortPreview.value || shortAction.value !== 'cover') return
  const order = shortQuoteOrder.value, generation = shortAccountGeneration
  if (!order) return
  shortQuoting.value = true
  try {
    const result = await fxApi.quoteShort(order.pairId, order.body)
    if (generation === shortAccountGeneration && shortQuoteOrder.value === order) shortQuote.value = result
  } catch (e) {
    if (generation === shortAccountGeneration) shortQuoteError.value = mapFxError(e, '回补报价失败')
  } finally { if (generation === shortAccountGeneration) shortQuoting.value = false }
}
const shortAveragePrice = computed(() =>
  shortQuote.value ? fxShortGoldPerForeign(shortQuote.value) : null,
)
const canFillShortOpen = computed(
  () =>
    !submitting.value &&
    !pendingRisk.value &&
    !shortStorageBlocked.value &&
    !!currentPricing.value && tradingSession.ready,
)
const canFillCoverAll = computed(
  () => !submitting.value && !pendingRisk.value && !shortStorageBlocked.value,
)
const canFillCover = computed(
  () =>
    canFillCoverAll.value &&
    !shortLoading.value &&
    !shortFailed.value &&
    shortPosition.value?.pair_id === pairId.value &&
    compareFxAmounts(shortPosition.value?.pending_short_debt, '0') === 1,
)
function shortBlockMessage(reason: string) {
  const messages: Record<string, string> = {
    insufficient_initial_margin: '开空后保证金率低于开仓门槛，请减少数量，或先减仓 / 还款。',
    credit_frozen: '账户已冻结新增信用；仍可按本笔报价尝试回补减仓。',
    frozen_by_operator: '当前暂停新增信用风险，仍可按本笔报价尝试回补。',
    spot_position_exists: '你持有这个币种的现货，需先卖出现货才能开空。',
    short_lending_limit: '该币种剩余借出额度不足，请减少开空数量。',
    insufficient_treasury_foreign: '可借外币库存不足，请减少开空数量。',
    insufficient_cash: '回补资金不足，请减少数量或补充现金。',
    'insufficient cash to cover': '回补资金不足，请减少数量或补充现金。',
    no_outstanding_short: '当前没有待回补欠币。',
    foreign_amount_exceeds_outstanding: '回补数量超过当前欠币，可选择全部回补。',
    insufficient_pool_foreign: '市场外币容量不足，请减少回补数量。',
    pair_not_open: '当前币种未开放新增做空。',
    pair_not_coverable: '当前币种暂停回补，请稍后重试。',
    fx_disabled: '外汇交易暂未开启。',
    unified_credit_disabled: '统一信贷暂未开启，不能进行空头交易。',
    loans_disabled: '借款功能暂停，暂不能新增空头。',
    short_disabled: '新增空头暂未开启。',
    tos_required: '请先同意用户协议。',
    version_conflict: '行情或账户已变化，请刷新报价。',
    incomplete_asset_valuation: '部分资产暂无法估值；新增风险暂停，回补以本笔报价为准。',
    risk_engine_unavailable: '风险检查暂不可用；回补以本笔报价可执行性为准。',
  }
  return messages[reason] ?? '暂无法确认本笔风险或交易条件，请刷新报价后重试。'
}
function updateShortGoldAmount(gold: string) {
  shortGoldAmount.value = gold
  shortAmount.value = ''
  if (!canFillShortOpen.value || !/^\d+(\.\d{0,6})?$/.test(gold.trim())) return
  const quantity = divideFxAmount(gold, localReferencePrice.value, 6)
  if (quantity == null || compareFxAmounts(quantity, '0') !== 1) {
    return
  }
  shortAmount.value = quantity
}
function setShortSizeUnit(unit: 'gold' | 'foreign') {
  if (submitting.value || pendingRisk.value) return
  if (unit === 'gold')
    shortGoldAmount.value = snapshot.value
      ? (multiplyFxAmount(shortAmount.value, localReferencePrice.value ?? '0') ?? '')
      : ''
  shortSizeUnit.value = unit
}
function fillCoverPortion(percent: number) {
  if (percent === 100) {
    if (!canFillCoverAll.value) return
    if (coverAll.value && shortAmount.value === '') {
      void fetchShortQuote()
      return
    }
    coverAll.value = true
    shortAmount.value = ''
  } else {
    if (!canFillCover.value || !shortPosition.value?.pending_short_debt) return
    const quantity = computeMinOut(shortPosition.value.pending_short_debt, (100 - percent) * 100)
    if (compareFxAmounts(quantity, '0') !== 1) {
      msg.info('回补比例不足最小外币数量，可选择全部回补')
      return
    }
    coverAll.value = false
    shortAmount.value = quantity
  }
}
const shortExpired = computed(
  () => !!shortQuote.value && now.value >= Date.parse(shortQuote.value.expires_at),
)
const shortLimit = computed(() =>
  !shortQuote.value
    ? ''
    : shortAction.value === 'open'
      ? computeMinOut(shortQuote.value.output_amount, effectiveSlippageBps.value)
      : computeMaxGoldIn(shortQuote.value.input_amount ?? '', effectiveSlippageBps.value),
)
const shortCanSubmit = computed(
  () =>
    shortValid.value &&
    !!shortQuote.value?.executable &&
    shortQuote.value.affordable !== false &&
    !!shortLimit.value &&
    !shortExpired.value &&
    !submitting.value &&
    !shortQuoting.value &&
    !pendingRisk.value &&
    !shortStorageBlocked.value,
)
const shortClosePreview = computed(() =>
  !shortQuoting.value &&
  !shortExpired.value &&
  !shortLoading.value &&
  !shortFailed.value &&
  shortPosition.value?.pair_id === pairId.value &&
  shortQuote.value?.pair_id === pairId.value
    ? fxShortClosePreview(shortQuote.value, shortPosition.value)
    : null,
)
const shortCloseUnavailable = computed(() => {
  if (shortExpired.value) return '报价已过期，重新报价后显示盈亏。'
  if (shortLoading.value) return '正在更新空头持仓…'
  if (shortFailed.value) return '持仓成本更新失败，请重新加载。'
  if (!shortQuote.value || shortQuoting.value) return '等待本笔回补报价。'
  return '剩余开空所得或含息欠币暂不可确认。'
})
const spotSubmitLabel = computed(() => {
  if (submitting.value) return '提交中…'
  if (pendingRisk.value) return '订单结果待确认'
  if (!amountValid.value) return side.value === 'buy' ? '买入' : '卖出'
  if (tradeBlockReason.value) return '暂不能成交'
  if (quoting.value || !quote.value) return '报价中…'
  if (quoteExpired.value) return '报价已过期'
  if (newShortLeg.value) return crossesPosition.value ? '卖出后做空' : '借币做空'
  if (allowBorrow.value) return '借款买入'
  if (side.value === 'sell' && spotClosePreview.value?.full) return '卖出全部'
  return side.value === 'buy' ? '买入' : '卖出'
})
const shortSubmitLabel = computed(() => {
  if (submitting.value) return '提交中…'
  if (pendingRisk.value) return '订单结果待确认'
  if (shortStorageBlocked.value) return '暂不能提交'
  if (!shortValid.value) return shortAction.value === 'open' ? '借币做空' : '回补'
  if (shortExpired.value) return '报价已过期'
  if (shortQuoteError.value) return '报价失败'
  if (shortQuoting.value || !shortQuote.value) return '报价中…'
  if (!shortQuote.value.executable || shortQuote.value.affordable === false) return '暂不能成交'
  return shortAction.value === 'open' ? '借币做空' : coverAll.value ? '全部回补' : '回补'
})
const spotSubmitSummary = computed(() => {
  const q = quote.value
  if (!q || !amountValid.value || tradeBlockReason.value || quoting.value || quoteExpired.value || submitting.value || pendingRisk.value) return null
  return {
    foreign: side.value === 'buy' ? q.output_amount : q.input_amount,
    gold: side.value === 'buy' ? q.input_amount : q.output_amount,
    receiving: side.value === 'sell',
  }
})
const shortSubmitSummary = computed(() => {
  const q = shortQuote.value
  if (!q || !shortCanSubmit.value || submitting.value || pendingRisk.value) return null
  const foreign = q.actual_foreign_amount
  const gold = shortAction.value === 'open' ? q.restricted_gold_delta : q.input_amount
  return foreign != null && gold != null ? { foreign, gold, receiving: shortAction.value === 'open' } : null
})
const pendingShortInterest = computed(() => {
  const row = shortPosition.value
  if (!row || row.pending_short_debt == null) return null
  const accrued = subtractFxAmounts(row.pending_short_debt, row.principal_foreign)
  return accrued == null ? null : subtractFxAmounts(accrued, row.interest_foreign)
})

const hasShortHolding = computed(
  () =>
    shortPosition.value?.pair_id === pairId.value &&
    compareFxAmounts(shortPosition.value.pending_short_debt, '0') === 1,
)
const fundingAmounts = computed(() =>
  fxNetFundingPreview(
    amount.value,
    summary.value,
    shortPosition.value?.pair_id === pairId.value ? shortPosition.value : null,
    null,
  ),
)
const fundingPreview = computed(() =>
  tradeMode.value === 'spot' &&
  side.value === 'buy' &&
  !summaryLoading.value &&
  !summaryFailed.value
    ? fxNetFundingPreview(
        amount.value,
        summary.value,
        shortPosition.value?.pair_id === pairId.value ? shortPosition.value : null,
        quote.value,
      )
    : null,
)
const orderPrincipal = computed(() =>
  summaryLoading.value || summaryFailed.value ? null : fxOrderPrincipal(summary.value),
)
const orderMultiplierLimit = computed(() => {
  const limit = summary.value?.credit_leverage
  return !summaryLoading.value &&
    !summaryFailed.value &&
    limit != null &&
    Number.isFinite(limit) &&
    limit > 1 &&
    limit <= 50
    ? limit
    : null
})
const orderHeading = computed(() =>
  tradeMode.value === 'short'
    ? shortAction.value === 'cover'
      ? '回补空头'
      : '新增空头'
    : quote.value
      ? fxNetActionLabel(quote.value.legs)
      : side.value === 'buy'
        ? '买入'
        : '卖出',
)
const accountRisk = computed(() =>
  resolveCreditRiskStatus({
    authoritativeStatus:
      summaryLoading.value || summaryFailed.value ? 'unknown' : summary.value?.risk_status,
    ratio:
      summaryLoading.value || summaryFailed.value
        ? null
        : (summary.value?.equity_to_risk_basis ?? null),
    noRisk:
      !summaryLoading.value &&
      !summaryFailed.value &&
      compareFxAmounts(summary.value?.risk_basis, '0') === 0,
  }),
)
const riskLabels = {
  healthy: '健康',
  warning: '低于开仓门槛',
  danger: '有强平风险',
  blocked: '风险检查受阻',
  protected: '保护中',
  none: '无借款风险占用',
  unknown: '数据待恢复',
  unestimated: '未生成风险预估',
}
const accountRiskLabel = computed(() => riskLabels[accountRisk.value])
const maintenancePercent = computed(() => {
  const ratio = tradingSession.rules?.maintenance_ratio
  return ratio == null ? '—' : `${displayAmount(multiplyFxAmount(ratio, '100'), 2)}%`
})
const currentMarginDisplay = computed(() => {
  const account = tradingSession.accountPreview
  if (!tradingSession.ready || !account || account.risk_status === 'unknown') return '暂无法确认'
  if (account.has_debt === false) return '无负债'
  return fxMarginPercent(account.liquidation_equity, account.risk_basis)
})
const postTradeMarginDisplay = computed(() => {
  if (tradeMode.value === 'short') {
    const q = shortQuote.value
    if (!q) return '—'
    if (shortExpired.value) return '报价已过期'
    if (q.margin_status === 'blocked') return '暂无法确认'
    if (localShortPreview.value?.preview.account_preview?.has_debt === false) return '无负债'
    return fxMarginPercent(q.estimated_equity, q.estimated_risk_basis)
  }
  const account = localOrderPreview.value?.account_preview
  if (!account) return '—'
  if (quoteExpired.value) return '报价已过期'
  if (!tradingSession.ready || account.risk_status === 'unknown') return '暂无法确认'
  if (account.has_debt === false) return '无负债'
  return fxMarginPercent(account.liquidation_equity, account.risk_basis)
})

function selectDirection(mode: 'spot' | 'short') {
  if (submitting.value || pendingRisk.value) return
  setSide(mode === 'spot' ? 'buy' : 'sell')
}
async function focusTradePanel() {
  await nextTick()
  const anchor = tradePanelRef.value
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  anchor?.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' })
  anchor?.focus({ preventScroll: true })
}
async function prepareShortCover(percent: number) {
  if (percent === 100 ? !canFillCoverAll.value : !canFillCover.value) return
  tradeMode.value = 'short'
  shortAction.value = 'cover'
  fillCoverPortion(percent)
  await focusTradePanel()
}
async function openFxTrade(next: FxSide) {
  setSide(next)
  await focusTradePanel()
}
// Shared baseline owns all pair balances; this projection is local display only.
function syncAccountDisplay() {
  const a = tradingSession.account, r = tradingSession.rules, p = tradingSession.accountPreview
  summaryLoading.value = !a; walletLoading.value = !a; shortLoading.value = !a
  summaryFailed.value = tradingSession.status === 'error'
  walletFailed.value = summaryFailed.value; shortFailed.value = summaryFailed.value
  if (!a || !r || !p || a.user_id !== authStore.user?.id) return
  const risk = !tradingSession.ready || p.risk_status === 'unknown' ? 'blocked'
    : p.maintenance_condition === 'rejected' ? 'danger' : p.initial_condition === 'rejected' ? 'warning' : 'healthy'
  const fxMtm = a.fx_wallets.reduce((n, w) => n + Number(p.group_values[`fx:${w.pair_id}`]?.mtm ?? 0), 0)
  const fxBasis = a.fx_wallets.reduce((n, w) => n + Number(w.cost_basis), 0)
  summary.value = {
    cash: Number(a.cash), debt: Number(a.debt), debt_with_interest: p.gold_debt == null ? null : Number(p.gold_debt),
    available_cash: p.available_cash,
    display_equity: p.display_equity == null ? null : Number(p.display_equity),
    liquidation_equity: p.liquidation_equity == null ? null : Number(p.liquidation_equity), risk_basis: p.risk_basis,
    equity_to_risk_basis: p.liquidation_equity && p.risk_basis && compareFxAmounts(p.risk_basis, '0') === 1
      ? Number(divideFxAmount(p.liquidation_equity, p.risk_basis, 12)) : null,
    risk_status: risk, blocked_reason: p.unknown_reasons.join('、'), credit_frozen: a.credit_frozen,
    new_risk_frozen: r.credit_new_risk_frozen, credit_leverage: r.leverage == null ? null : Number(r.leverage),
    r_initial: r.initial_ratio == null ? null : Number(r.initial_ratio),
    r_maintenance: r.maintenance_ratio == null ? null : Number(r.maintenance_ratio),
    positions: a.lmsr_positions.filter(v => v.market_id != null).map(v => ({ market_id: v.market_id!, outcome_id: v.outcome_id, amount: Number(v.amount), cost_basis: Number(v.cost_basis) })),
    fx_mtm: fxMtm, fx_cost_basis: fxBasis, fx_unrealized_pnl: fxMtm - fxBasis,
    sell_fee_rate: Number(r.lmsr_sell_fee_rate), rank_thresholds: [], last_liquidated_at: a.last_liquidated_at,
  }
  const pid = pairId.value
  if (!pid) return
  wallet.value = a.fx_wallets.find(v => v.pair_id === pid) ?? { pair_id: pid, foreign_amount: '0', cost_basis: '0' }
  walletHoldingGeneration.value = holdingAuthGeneration.value; shortHoldingGeneration.value = holdingAuthGeneration.value
  const row = a.fx_shorts.find(v => v.pair_id === pid)
  if (!row) { shortPosition.value = null; return }
  let debt: string | null = null, cost: string | null = null, fee: string | null = null
  try {
    debt = accrueTradingDebt(addFxAmounts(row.principal_foreign, row.interest_foreign) ?? 'invalid', row.interest_last_accrued_at, r.loan_daily_rate, tradingSession.now).amount
    if (currentPricing.value && compareFxAmounts(debt, '0') === 1) {
      const cover = quoteFx('buyExactOut', debt, currentPricing.value)
      cost = cover.input_amount; fee = cover.fee_amount
    }
  } catch { /* Unsupported accrued debt stays unknown. */ }
  shortPosition.value = { ...row, currency_code: activePair.value?.currency_code ?? '', pending_short_debt: debt,
    reference_cover_cost: cost, reference_cover_fee: fee, executable: false,
    risk_status: risk, blocked_reason: p.unknown_reasons.join('、') || null }
}
watch([() => tradingSession.accountPreview, () => tradingSession.ready, () => tradingSession.status, pairId], syncAccountDisplay)
watch(
  [shortAction, shortAmount, coverAll, pairId],
  () => {
    shortError.value = ''
  },
  { flush: 'sync' },
)
function syncPendingOrders() {
  netPending.value = netSubmitter.pending
  netUnresolved.value = netSubmitter.hasUnresolved
  netStorageBlocked.value = netSubmitter.storageError
  borrowPending.value = borrowSubmitter.pending
  borrowUnresolved.value = borrowSubmitter.hasUnresolved
  borrowStorageBlocked.value = borrowSubmitter.storageError
  pendingShort.value = shortSubmitter.pending
  shortUnresolved.value = shortSubmitter.hasUnresolved
  shortStorageBlocked.value = shortSubmitter.storageError || !authStore.user?.id
}
async function sendBorrowBuy(run: () => Promise<FxBorrowBuyTrade | null>) {
  if (submitting.value) return
  const generation = borrowGeneration
  submitting.value = true
  borrowError.value = ''
  let completed = false
  try {
    const promise = run()
    syncPendingOrders()
    const result = await promise
    if (!result || generation !== borrowGeneration) return
    completed = true
    borrowReceipt.value = {
      trade: result,
      currency:
        pairs.value.find((p) => p.id === result.pair_id)?.currency_name ??
        `外币（${result.pair_id}）`,
    }
    amount.value = ''
    syncPendingOrders()
    msg.success(result.replay ? '已确认原融资成交' : '借款并买入已成交')
    await refreshAll()
    if (generation !== borrowGeneration) return
    chartReloadToken.value++
    if (summaryFailed.value || walletFailed.value || tradesFailed.value || snapshotFailed.value)
      borrowError.value = '借款买入已完成，部分账户或行情信息刷新失败，请更新账户后再下单。'

  } catch (e) {
    if (generation !== borrowGeneration) return
    borrowError.value = completed
      ? '借款买入已完成，账户信息刷新失败。'
      : borrowSubmitter.pending
        ? '成交结果尚未确认，请重试原订单核对同一笔成交；期间不能提交新的交易。'
        : mapFxError(e, '借款买入失败，请重新报价')
  } finally {
    if (generation === borrowGeneration) {
      syncPendingOrders()
      submitting.value = false
    }
  }
}
function retryBorrowBuy() {
  if (!borrowPending.value || submitting.value) return
  void sendBorrowBuy(() => borrowSubmitter.retry((r) => fxApi.borrowBuy(r.pairId, r.body)))
}
async function submitShort() {
  if (submitting.value || pendingRisk.value) return
  if (shortExpired.value) {
    await fetchShortQuote()
    shortError.value = '报价已刷新，请确认新价格后再次提交'
    return
  }
  if (!shortCanSubmit.value || !pairId.value) return
  const pid = pairId.value
  const action = shortAction.value
  const quantity = shortAmount.value.trim()
  const all = action === 'cover' && coverAll.value
  const q = shortQuote.value
  if (
    !q ||
    !q.executable ||
    q.pair_id !== pid ||
    q.action !== action ||
    !!q.cover_all !== all ||
    (!all && compareFxAmounts(q.requested_foreign_amount, quantity) !== 0)
  ) {
    shortQuote.value = null
    shortError.value = '订单已变化，请重新报价后确认'
    return
  }
  const limit = shortLimit.value
  const body =
    action === 'open'
      ? { foreign_amount: quantity, min_gold_out: limit }
      : { ...(all ? { cover_all: true } : { foreign_amount: quantity }), max_gold_in: limit }
  const execute = () => sendShortRequest(() => shortSubmitter.start(pid, action, body, executePendingShort))
  if (action === 'open') {
    const account = localShortPreview.value?.preview.account_preview
    await borrowConfirmation.request({
      kind: 'short', currency: currencyName.value, borrowAmount: quantity, cashInput: null,
      quantity, lockedGold: q.restricted_gold_delta, equity: q.estimated_equity,
      basis: q.estimated_risk_basis, maintenance: tradingSession.rules?.maintenance_ratio ?? null,
      reliable: !!account && account.risk_status !== 'unknown',
    }, Date.parse(q.expires_at), execute)
  } else await execute()
}
function executePendingShort(request: FxPendingShortRequest) {
  return request.action === 'open'
    ? fxApi.openShort(request.pairId, {
        foreign_amount: request.body.foreign_amount!,
        min_gold_out: request.body.min_gold_out!,
        idempotency_key: request.body.idempotency_key,
      })
    : fxApi.coverShort(request.pairId, { ...request.body, max_gold_in: request.body.max_gold_in! })
}
async function retryShort() {
  if (submitting.value || !pendingShort.value) return
  await sendShortRequest(() => shortSubmitter.retry(executePendingShort))
}
async function sendShortRequest(run: () => Promise<FxShortTrade | null>) {
  const account = authStore.user?.id,
    generation = shortAccountGeneration
  const current = () => account === authStore.user?.id && generation === shortAccountGeneration
  let completed = false
  submitting.value = true
  try {
    const promise = run()
    syncPendingOrders()
    const result = await promise
    if (!result || !current()) return
    completed = true
    shortReceipt.value = result
    shortQuote.value = null
    shortAmount.value = ''
    shortGoldAmount.value = ''
    shortError.value = ''
    syncPendingOrders()
    msg.success(result.purpose === 'short_open' ? '开空已成交，所得已锁定用于回补' : '回补已成交')
    await refreshAll()
    if (!current()) return
    chartReloadToken.value++
    if (summaryFailed.value || walletFailed.value || tradesFailed.value || shortFailed.value)
      shortError.value = '交易已成交，部分账户信息刷新失败。'
  } catch (e) {
    if (!current()) return
    shortQuote.value = null
    shortError.value = completed
      ? '交易已成交，账户信息刷新失败。'
      : shortSubmitter.pending
        ? '成交结果尚未确认，请恢复原请求核对同一笔成交；期间不能提交新的交易。'
        : mapFxError(e, '空头成交失败，请重新报价')
  } finally {
    if (current()) {
      syncPendingOrders()
      submitting.value = false
    }
  }
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

function startAccountSession() {
  if (pairId.value) tradingLease = tradingSession.start({ product: 'fx', id: pairId.value })
}
async function refreshAll() {
  startAccountSession()
  tradingSession.requestRefresh(undefined, 'fx_refresh')
  await Promise.allSettled([loadSnapshot(), loadTrades()])
}

// ── SSE ──
let stream: FxStream | null = null
function onEnvelope(envelope: FxPublicEnvelope) {
  const current = snapshot.value
  if (current) {
    const prev = Number(current.price)
    const next = envelope.price !== undefined ? Number(envelope.price) : null
    if (next !== null && Number.isFinite(next) && Number.isFinite(prev)) {
      if (next > prev) priceDirection.value = 'up'
      else if (next < prev) priceDirection.value = 'down'
    }
    snapshot.value = {
      ...current,
      price: envelope.price ?? current.price,
      buy_price: envelope.buy_price ?? current.buy_price,
      sell_price: envelope.sell_price ?? current.sell_price,
      spread: envelope.spread ?? current.spread,
      volume_24h: envelope.volume ?? current.volume_24h,
    }
  }
  if (envelope.price !== undefined) {
    marketUpdatedAt.value = new Date().toLocaleTimeString()
  }
  chartEnvelope.value = envelope
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
        void loadSnapshot()
        chartReloadToken.value += 1
      }
    })
    stream.onError(() => {
      streamFailed.value = true
      streamConnected.value = false
    })
    stream.onEnvelope(onEnvelope)
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
  shortReceipt.value = null
  shortQuote.value = null
  shortPosition.value = null
  shortFailed.value = false
  shortAmount.value = ''
  shortGoldAmount.value = ''
  shortError.value = ''
  pairId.value = id
  amount.value = ''
  quote.value = null
  tradeError.value = null
  walletResource.reset()
  tradeResource.reset()
  receipt.value = null
  snapshot.value = null
  chartEnvelope.value = null
  try {
    startAccountSession()
    await Promise.all([loadSnapshot(), loadTrades()])
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
    startAccountSession()
    await Promise.all([loadSnapshot(), loadTrades()])
    connectStream()
  } catch (e) {
    error.value = mapFxError(e, 'FX 行情加载失败')
  } finally {
    loading.value = false
  }
}

// Preview the required financing; permission is granted only when submitting this captured order.
const netOrder = computed(() =>
  pairId.value && authStore.user?.id && tradeMode.value === 'spot' && amountValid.value && !pendingRisk.value
    ? { pairId: pairId.value, body: { side: side.value, amount: amount.value.trim(),
        borrow_amount: side.value === 'buy' ? fundingAmounts.value?.shortfall ?? '0' : '0' } } : null,
)
const localOrderPreview = computed(() => netOrder.value
  ? tradingSession.previewFx({ ...netOrder.value.body, allow_growth: !closeOnly.value }, netOrder.value.pairId) : null)
watch(localOrderPreview, (p) => {
  const order = netOrder.value, pricing = currentPricing.value
  const economicVersion = safeEconomicVersion(tradingSession.account?.economic_version)
  const poolVersion = safeEconomicVersion(pricing?.pool_version)
  quoteReferencePrice.value = localReferencePrice.value; quoting.value = false
  if (!p || !order || !pricing || economicVersion == null || poolVersion == null) {
    quote.value = null
    tradeError.value = economicVersion == null && tradingSession.account ? '账户版本超出安全范围，暂不能提交。' : null
    return
  }
  let g = pricing.gold_reserve, f = pricing.foreign_reserve
  const legs = p.legs.map(leg => {
    const result = { ...leg, input_currency: leg.side === 'buy' ? 'GOLD' : activePair.value?.currency_code ?? 'FOREIGN',
      output_currency: leg.side === 'sell' ? 'GOLD' : activePair.value?.currency_code ?? 'FOREIGN',
      pre_gold_reserve: g, pre_foreign_reserve: f }
    g = leg.post_gold_reserve; f = leg.post_foreign_reserve
    return result
  })
  quote.value = {
    pair_id: order.pairId, side: order.body.side, input_amount: order.body.amount,
    output_amount: p.output_amount, fee_amount: p.fee_amount, post_price: divideFxAmount(g, f, 12) ?? '0', legs,
    final_direction: p.final_direction, wallet_after: p.wallet_after, foreign_debt_after: p.foreign_debt_after,
    cash_delta: p.cash_delta, gold_debt_delta: p.gold_debt_delta, restricted_gold_delta: p.restricted_gold_delta,
    growth_amount: p.growth_amount, borrow_amount: order.body.borrow_amount, repaid_gold: p.repaid_gold,
    executable: p.condition === 'satisfied' || (closeOnly.value && p.condition === 'unknown' && p.rejected_reasons.length === 0 && compareFxAmounts(p.growth_amount, '0') === 0),
    blocked_reason: p.rejected_reasons[0] ?? (p.condition === 'unknown' ? p.unchecked_reasons[0] ?? '估值未知' : null),
    economic_version: economicVersion, pool_version: poolVersion, expires_at: new Date(Date.now() + 30000).toISOString(),
  }
  now.value = Date.now()
}, { flush: 'sync' })
async function fetchQuote() {
  await tradingSession.checkVersions()
  if (localOrderPreview.value || !closeOnly.value) return
  const order = netOrder.value, account = authStore.user?.id
  if (!order) return
  quoting.value = true
  try {
    const result = await fxNetApi.quote(order.pairId, order.body)
    if (account === authStore.user?.id && netOrder.value === order && closeOnly.value && compareFxAmounts(result.growth_amount, '0') === 0) quote.value = result
  } catch (e) {
    if (account === authStore.user?.id) tradeError.value = mapFxError(e, '减仓报价失败')
  } finally { if (account === authStore.user?.id) quoting.value = false }
}

async function sendNet(run: () => Promise<FxNetTrade | null>) {
  const account = authStore.user?.id
  submitting.value = true
  tradeError.value = null
  let completed = false
  try {
    const promise = run()
    syncPendingOrders()
    const result = await promise
    if (!result || account !== authStore.user?.id) return
    completed = true
    receipt.value = {
      trade: result,
      currency: pairs.value.find((p) => p.id === result.pair_id)?.currency_name ?? '外币',
    }
    syncPendingOrders()
    amount.value = ''
    msg.success(result.replay ? '已确认原成交' : `${fxNetActionLabel(result.legs)}已成交`)
    await refreshAll()
    if (account !== authStore.user?.id) return
    chartReloadToken.value++
    if (summaryFailed.value || walletFailed.value || tradesFailed.value || shortFailed.value)
      tradeError.value = '交易已完成，部分账户信息刷新失败，请更新账户后再下单。'
  } catch (e) {
    if (account !== authStore.user?.id) return
    tradeError.value = completed
      ? '交易已完成，账户信息刷新失败。'
      : netSubmitter.pending
        ? '成交结果尚未确认，请恢复原订单核对同一笔成交；期间不能提交新的交易。'
        : mapFxError(e, '成交失败，请重新报价')
  } finally {
    if (account === authStore.user?.id) {
      syncPendingOrders()
      submitting.value = false
    }
  }
}
async function submitTrade() {
  const q = quote.value,
    order = netOrder.value
  if (
    submitting.value ||
    pendingRisk.value ||
    !order ||
    !q ||
    !q.executable ||
    quoteExpired.value ||
    tradeBlockReason.value
  )
    return
  const body = {
    ...order.body,
    min_out: computeMinOut(q.output_amount, effectiveSlippageBps.value),
    expected_economic_version: q.economic_version,
    allow_growth: fxNetAllowsGrowth(q, closeOnly.value),
  }
  const execute = () => sendNet(() => netSubmitter.start(order.pairId, body, (r) => fxNetApi.trade(r.pairId, r.body)))
  const borrowing = fxNetBorrowAction(q)
  if (borrowing) {
    const account = localOrderPreview.value?.account_preview
    await borrowConfirmation.request({
      ...borrowing, currency: currencyName.value,
      cashInput: fundingPreview.value?.cashInput ?? null,
      lockedGold: borrowing.kind === 'short' ? borrowing.lockedGold : fundingPreview.value?.lockedInput ?? null,
      equity: account?.liquidation_equity ?? null, basis: account?.risk_basis ?? null,
      maintenance: tradingSession.rules?.maintenance_ratio ?? null,
      reliable: !!account && account.risk_status !== 'unknown',
    }, Date.parse(q.expires_at), execute)
  } else await execute()
}
async function retryNet() {
  if (submitting.value || !netPending.value) return
  await sendNet(() => netSubmitter.retry((r) => fxNetApi.trade(r.pairId, r.body)))
}
const groupedTrades = computed(() => fxGroupPersonalTrades(trades.value ?? []))

function setSide(next: FxSide) {
  if (submitting.value) return
  tradeMode.value = 'spot'
  closeOnly.value = false
  if (side.value === next) return
  amount.value = ''
  side.value = next
}

async function openShortTrade() {
  if (submitting.value || pendingRisk.value) return
  if (hasSpotHolding.value) await prepareSpotSellAll(100)
  else if (hasShortHolding.value) await prepareShortCover(100)
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

onMounted(() => {
  window.addEventListener('resize', resizeWorkbench)
  void load().then(() => {
    // A cover deep link already initialized cover_all; only locate its form.
    if (route.query.action === 'cover') void focusTradePanel()
    else if (route.query.action === 'open') void focusTradePanel()
  })
  freshnessTimer = setInterval(() => {
    now.value = Date.now()
  }, 1000)
})

onUnmounted(() => {
  window.removeEventListener('resize', resizeWorkbench)
  workbenchObserver?.disconnect()
  if (tradingLease !== undefined) tradingSession.stop(tradingLease)
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
          <span v-if="activePair" class="fx-status" :class="`fx-status-${activePair.status}`">{{
            activePair.reduce_only && ['trading', 'paused'].includes(activePair.status)
              ? '仅可减仓'
              : statusLabel
          }}</span>
        </div>
      </div>
      <div class="fx-topbar-price">
        <span class="fx-topbar-label">1 {{ currencyName }} 的金圆券价格</span
        ><strong class="fx-price" :class="priceDirection">{{
          formatFxPrice(snapshot?.price)
        }}</strong>
      </div>
      <div v-if="activePair && !loading" class="fx-account-bar" aria-label="账户概览">
        <div>
          本金（清算净值）
          <strong>{{
            summaryLoading
              ? '刷新中…'
              : summaryFailed
                ? '更新失败'
                : displayAmount(summary?.liquidation_equity, 2)
          }}</strong>
        </div>
        <div>
          可用现金
          <strong>{{
            summaryLoading
              ? '刷新中…'
              : summaryFailed
                ? '更新失败'
                : displayAmount(fxAvailableCash(summary), 2)
          }}</strong>
        </div>
        <div class="fx-account-margin" :class="{ 'fx-account-margin--warning': ['warning', 'danger', 'blocked', 'unknown'].includes(accountRisk) }">
          当前保证金率
          <strong>{{ currentMarginDisplay }}</strong>
          <span>{{ accountRiskLabel }}</span>
        </div>
        <div class="fx-account-maintenance">
          维持门槛
          <strong>{{ maintenancePercent }}</strong>
          <span>低于此值会触发强平</span>
        </div>
      </div>
      <div class="fx-market-volume">
        <span>24 小时成交额</span
        ><strong>{{ displayAmount(snapshot?.volume_24h, 2) }} <small>金圆券</small></strong>
      </div>
      <span class="fx-connection" :class="{ connected: streamConnected }" role="status"
        ><span class="fx-stream-dot" :class="{ on: streamConnected }" aria-hidden="true"></span
        >{{ streamLabel }}</span
      >
    </header>
    <div class="fx-freshness-bar">
      <span>{{ marketUpdatedAt ? `行情更新于 ${marketUpdatedAt}` : '等待行情数据' }}</span>
      <span v-if="snapshotFailed" class="fx-error" role="alert">行情刷新失败，显示上次数据。</span>
      <span v-else-if="streamFailed" class="fx-hint">下单前请刷新报价。</span>
      <button
        class="fx-text-button"
        :disabled="loading || snapshotLoading || submitting || !pairId"
        @click="refreshMarket"
      >
        {{ snapshotLoading ? '刷新中…' : '刷新行情与账户' }}
      </button>
    </div>

    <section v-if="pendingRisk" class="fx-receipt" role="status">
      <p v-if="netStorageBlocked || borrowStorageBlocked || shortStorageBlocked" class="fx-error">
        本地订单记录无法读取、保存或清除，请核对记录后再尝试清除。浏览器存储仍不可用时，请恢复存储并刷新页面。
      </p>
      <p>交易暂时受阻。可以先重试原订单；记录无法恢复时，请核对成交记录和账户余额，确认不清楚可联系管理员。</p>
      <router-link to="/user/transactions" class="fx-text-button">查看成交记录</router-link>
      <p>清除本地记录不会撤销成交或修改账户。</p>
      <label class="fx-field fx-field--inline">
        <input v-model="pendingRecordsReviewed" type="checkbox" :disabled="submitting" />
        <span>我已核对成交记录和账户</span>
      </label>
      <button class="btn-secondary" :disabled="submitting || !pendingRecordsReviewed" @click="clearReviewedOrders">
        已核对记录，清除本地待确认订单
      </button>
    </section>

    <div v-if="borrowPending" class="fx-pending-short" role="status">
      <div>
        <strong>一笔借款买入订单尚未确认</strong>
        <p>
          货币对 {{ borrowPending.pairId }}，投入
          {{ displayAmount(borrowPending.body.amount) }} 金圆券， 新增借款
          {{ displayAmount(borrowPending.body.borrow_amount) }}。重试沿用原订单编号。
        </p>
      </div>
      <button class="btn-secondary" :disabled="submitting" @click="retryBorrowBuy">
        {{ submitting ? '核对中…' : '重试原订单' }}
      </button>
    </div>
    <p v-if="borrowError" class="fx-error" role="status">{{ borrowError }}</p>
    <section v-if="borrowReceipt" class="fx-receipt" role="status">
      <h2>{{ borrowReceipt.trade.replay ? '原借款买入已确认' : '借款并买入已成交' }}</h2>
      <p>
        使用现金 {{ displayAmount(borrowReceipt.trade.cash_amount) }}，新增借款
        {{ displayAmount(borrowReceipt.trade.borrow_amount) }} 金圆券， 实际买入
        {{ displayAmount(borrowReceipt.trade.output_amount) }} {{ borrowReceipt.currency }}。
        手续费 {{ displayAmount(borrowReceipt.trade.fee_amount) }} 金圆券（已含）。
      </p>
    </section>
    <div v-if="loading" class="fx-state">行情加载中…</div>
    <div v-else-if="error" class="fx-state fx-state-error">
      {{ error }}<button class="btn-secondary" @click="load">重试</button>
    </div>
    <div v-else-if="!activePair" class="fx-state">外汇交易暂未开放，请稍后再来查看。</div>

    <template v-else>
      <div v-if="activePair.reduce_only" class="fx-notice">
        当前币种仅可减仓，请选择卖出多头或回补空头。
      </div>
      <div v-if="pendingShort" class="fx-pending-short" role="status">
        <div>
          <strong
            >一笔{{ pendingShort.action === 'open' ? '做空' : '空头平仓' }}订单尚未确认</strong
          >
          <p>
            货币对 {{ pendingShort.pairId }}，{{
              pendingShort.body.cover_all ? '全部平仓' : `${pendingShort.body.foreign_amount} 外币`
            }}。重试会核对同一笔订单，请勿重复开仓。
          </p>
        </div>
        <button class="btn-secondary" :disabled="submitting" @click="retryShort">
          {{ submitting ? '核对中…' : '重试原订单' }}
        </button>
      </div>

      <div
        ref="workbenchRef"
        class="fx-workbench"
        :style="{ '--fx-workbench-height': workbenchHeight }"
      >
        <div class="fx-market-column">
          <section class="fx-chart-panel" aria-label="价格走势">
            <div class="fx-panel-head">
              <h2>价格走势</h2>
              <div class="fx-intervals">
                <button
                  v-for="iv in intervals"
                  :key="iv"
                  class="fx-interval"
                  :class="{ active: interval === iv }"
                  :aria-pressed="interval === iv"
                  @click="setChartInterval(iv)"
                >
                  {{ iv === '1m' ? '1 分钟' : iv === '15m' ? '15 分钟' : '1 小时' }}
                </button>
              </div>
            </div>
            <div class="fx-chart-body">
              <FxCandleChart
                v-if="pairId"
                :pair-id="pairId"
                :interval="interval"
                :envelope="chartEnvelope"
                :reload-token="chartReloadToken"
                :holding-price-lines="holdingPriceLines"
                :trigger-orders="triggerOrders"
                height="100%"
              />
              <div class="fx-chart-order-entry">
                <FxTriggerOrders
                  v-if="activePair && !loading && !error"
                  :pair-id="activePair.id"
                  @waiting-change="waitingOrders = $event"
                />
              </div>
            </div>
            <div class="fx-chart-footer">
              <span>上涨有利于多头，下跌有利于空头</span><span>金圆券 / {{ currencyName }}</span>
            </div>
          </section>
        </div>

        <aside class="fx-trade-panel" aria-labelledby="fx-trade-heading">
          <h2 id="fx-trade-heading" ref="tradePanelRef" class="fx-trade-anchor" tabindex="-1">
            {{ tradeMode === 'short' ? '回补' : '买卖' }} {{ currencyName }}
          </h2>
          <div class="fx-direction-picker fx-action-picker" role="group" aria-label="选择交易操作">
            <button
              class="fx-direction-long"
              :class="{ active: tradeMode === 'spot' && side === 'buy' }"
              :aria-pressed="tradeMode === 'spot' && side === 'buy'"
              :disabled="submitting || pendingRisk"
              @click="selectDirection('spot')"
            >
              <span aria-hidden="true">↗</span><strong>买入</strong>
            </button>
            <button
              class="fx-direction-short"
              :class="{ active: tradeMode === 'spot' && side === 'sell' && !closeOnly }"
              :aria-pressed="tradeMode === 'spot' && side === 'sell' && !closeOnly"
              :disabled="submitting || pendingRisk"
              @click="selectDirection('short')"
            >
              <span aria-hidden="true">↘</span><strong>卖出</strong>
            </button>
            <button
              class="fx-close-choice fx-close-choice--sell"
              :class="{ active: tradeMode === 'spot' && closeOnly }"
              :aria-pressed="tradeMode === 'spot' && closeOnly"
              :disabled="!canSellSpotHolding"
              @click="prepareSpotSellAll(100)"
            >
              <strong>卖出多头</strong>
            </button>
            <button
              class="fx-close-choice"
              :class="{ active: tradeMode === 'short' && shortAction === 'cover' }"
              :aria-pressed="tradeMode === 'short' && shortAction === 'cover'"
              :disabled="
                !canFillCoverAll ||
                (!hasShortHolding && !shortFailed && shortPosition?.pending_short_debt !== null)
              "
              @click="prepareShortCover(100)"
            >
              <strong>回补空头</strong>
            </button>
          </div>
          <div class="fx-panel-scroll">
            <section class="fx-current-position" aria-label="当前币种持仓与快速平仓">
              <p v-if="walletLoading || shortLoading" class="fx-hint" role="status">
                正在更新持仓，以下为上次快照。
              </p>
              <p v-if="walletFailed || shortFailed" class="fx-error" role="alert">
                持仓更新失败，以下为上次快照。
              </p>
              <FxPositionCard
                v-if="hasSpotHolding" kind="long" :currency="currencyName"
                :quantity="wallet?.foreign_amount" :basis="wallet?.cost_basis"
                :close-amount="spotHoldingClose?.output_amount" :pnl="spotHoldingClosePnl"
                :disabled="!canSellSpotHolding" @close="prepareSpotSellAll(100)"
              />
              <FxPositionCard
                v-if="hasShortHolding" kind="short" :currency="currencyName"
                :quantity="shortPosition?.pending_short_debt" :basis="shortPosition?.proceeds_basis_gold"
                :close-amount="shortPosition?.reference_cover_cost" :pnl="shortHoldingPnl"
                :disabled="!canFillCoverAll" @close="prepareShortCover(100)"
              />
              <p
                v-if="!shortFailed && !shortLoading && shortPosition?.pending_short_debt === null"
                class="fx-hint"
              >
                欠币数量暂无法确认，可尝试全部回补。
              </p>
              <p v-else-if="!walletLoading && !shortLoading && !walletFailed && !shortFailed && tradingSession.account && !hasSpotHolding && !hasShortHolding" class="fx-no-position">暂无持仓</p>
              <p v-else-if="!tradingSession.account" class="fx-hint">正在加载持仓…</p>
            </section>
            <details v-if="hasSpotHolding || hasShortHolding" class="fx-position-details">
              <summary>持仓详情</summary>
              <div v-if="hasSpotHolding" class="fx-position-extra">
                <div class="fx-preview-row"><span>剩余持仓成本</span><strong>{{ displayAmount(wallet?.cost_basis, 2) }} 金圆券</strong></div>
                <div class="fx-preview-row"><span>账面市值</span><strong>{{ displayAmount(holdingValue?.marketValue, 2) }} 金圆券</strong></div>
                <div class="fx-preview-row"><span>账面盈亏</span><strong>{{ displayAmount(holdingValue?.pnl, 2) }} 金圆券</strong></div>
                <p>平仓预估包含手续费和价格影响，未扣金圆券借款利息。</p>
              </div>
              <div v-if="hasShortHolding" class="fx-position-extra">
                <div class="fx-preview-row"><span>锁定所得</span><strong>{{ displayAmount(shortPosition?.restricted_gold, 2) }} 金圆券</strong></div>
                <div class="fx-preview-row"><span>借币本金</span><strong>{{ displayAmount(shortPosition?.principal_foreign) }} {{ currencyName }}</strong></div>
                <div class="fx-preview-row"><span>已计／待计利息</span><strong>{{ displayAmount(shortPosition?.interest_foreign) }} / {{ displayAmount(pendingShortInterest) }} {{ currencyName }}</strong></div>
                <p>盈亏按剩余开空所得减去预计回补支出估算，包含利息、手续费和价格影响。</p>
              </div>
            </details>
            <div class="fx-order-heading">
              <h2 class="fx-trade-heading">
                {{ orderHeading }}
              </h2>
              <label v-if="(tradeMode === 'spot' && side === 'sell' && !closeOnly) || (tradeMode === 'short' && shortAction === 'open')" class="fx-short-unit"
                >输入方式
                <select
                  :value="tradeMode === 'spot' ? sellSizeUnit : shortSizeUnit"
                  aria-label="卖出规模输入方式"
                  :disabled="submitting || pendingRisk"
                  @change="
                    tradeMode === 'spot'
                      ? sellSizeUnit = ($event.target as HTMLSelectElement).value as 'gold' | 'foreign'
                      : setShortSizeUnit(($event.target as HTMLSelectElement).value as 'gold' | 'foreign')
                  "
                >
                  <option value="gold">按金额</option>
                  <option value="foreign">按数量</option>
                </select></label
              >
            </div>
            <FxOrderSize
              v-if="tradeMode === 'spot' && side === 'buy'"
              v-model="amount"
              :principal="orderPrincipal"
              :limit="orderMultiplierLimit"
              tone="long"
              label="买入金额（含费）"
              :disabled="!tradable || submitting || pendingRisk"
            />
            <FxOrderSize
              v-if="tradeMode === 'spot' && side === 'sell' && !closeOnly && sellSizeUnit === 'gold'"
              :model-value="sellGoldAmount" :principal="orderPrincipal" :limit="orderMultiplierLimit"
              tone="short" label="卖出参考金额"
              :disabled="!tradable || !tradingSession.ready || submitting || pendingRisk"
              @update:model-value="updateSellGoldAmount"
            />
            <FxOrderSize
              v-if="tradeMode === 'short' && shortAction === 'open' && shortSizeUnit === 'gold'"
              :model-value="shortGoldAmount"
              :principal="orderPrincipal"
              :limit="orderMultiplierLimit"
              tone="short"
              label="做空参考金额"
              :disabled="
                !canFillShortOpen ||
                hasSpotHolding ||
                activePair?.status !== 'trading' ||
                activePair?.reduce_only === true
              "
              @update:model-value="updateShortGoldAmount"
            />
            <input
              v-if="tradeMode === 'short' && shortAction === 'open' && shortSizeUnit === 'foreign'"
              id="fx-short-foreign"
              aria-label="借入并卖出数量"
              v-model="shortAmount"
              class="fx-input fx-size-input"
              inputmode="decimal"
              autocomplete="off"
              placeholder="0"
              :disabled="submitting || pendingRisk || shortStorageBlocked"
            />
            <div v-show="tradeMode === 'spot'" class="fx-trade-body">
              <template v-if="side === 'sell'">
                <label v-if="closeOnly || sellSizeUnit === 'foreign'" class="fx-field"
                  ><span
                    >卖出数量 <small>{{ currencyName }}</small></span
                  >
                  <input
                    v-model="amount"
                    class="fx-input fx-size-input"
                    inputmode="decimal"
                    autocomplete="off"
                    placeholder="0"
                    :disabled="!tradable || submitting || pendingRisk"
                  />
                </label>
                <div v-if="!closeOnly && sellSizeUnit === 'gold'" class="fx-preview-row"><span>卖出数量</span><strong>{{ displayAmount(amount) }} {{ currencyName }}</strong></div>
                <div v-if="hasSpotHolding" class="fx-sell-shortcuts">
                  <button
                    v-for="percent in portions"
                    :key="percent"
                    class="btn-secondary"
                    :disabled="!canFillSell"
                    @click="fillSellPortion(percent)"
                  >
                    {{ percent === 100 ? '全部' : `${percent}%` }}
                  </button>
                </div>
              </template>
              <div v-if="amountValid" class="fx-quote-box" aria-label="交易报价">
                <div v-if="side === 'buy'" class="fx-funding-preview">
                  <div>
                    <span>现金</span
                    ><strong>{{ displayAmount(fundingPreview?.cashInput, 2) }}</strong>
                  </div>
                  <div v-if="compareFxAmounts(fundingPreview?.lockedInput, '0') !== 0">
                    <span>锁金</span
                    ><strong>{{ displayAmount(fundingPreview?.lockedInput, 2) }}</strong>
                  </div>
                  <div v-if="compareFxAmounts(fundingPreview?.shortfall, '0') !== 0">
                    <span>借款</span
                    ><strong>{{
                      displayAmount(fundingPreview?.shortfall, 2)
                    }}</strong>
                  </div>
                </div>
                <div v-if="newShortLeg" class="fx-preview-row"><span>预计新增欠币</span><strong>{{ displayAmount(newShortLeg.input_amount) }} {{ currencyName }}</strong></div>
                <div v-if="side === 'buy'" class="fx-preview-row"><span>预计支付（含费）</span><strong>{{ displayAmount(quote?.input_amount, 2) }} 金圆券</strong></div>
                <FxExecutionPrice
                  :average="effectiveGoldPerForeign"
                  :reference="quoteReferencePrice"
                  :currency="currencyName"
                />
                <div class="fx-preview fx-order-summary">
                  <div v-if="!crossesPosition || side === 'sell'" class="fx-preview-row">
                    <span>{{ spotOutputLabel }}</span
                    ><strong
                      >{{ quote ? displayAmount(quote.output_amount) : '—' }}
                      {{ outputCurrency }}</strong
                    >
                  </div>
                  <div class="fx-quote-meta">
                    <div class="fx-preview-row">
                      <span>手续费（已含）</span
                      ><strong
                        >{{ quote ? displayAmount(quote.fee_amount) : '—' }}
                        {{ inputCurrency }}</strong
                      >
                    </div>
                  </div>
                </div>
                <FxClosePnl
                  v-if="side === 'sell' && hasSpotHolding"
                  :pnl="spotClosePreview?.pnl ?? null"
                  :note="
                    spotClosePreview?.full
                      ? '含报价手续费，未扣金圆券借款利息。'
                      : '按卖出数量分摊成本；含报价手续费，未扣金圆券借款利息。'
                  "
                  :unavailable="spotCloseUnavailable"
                />
              </div>
              <div v-if="quote && (crossesPosition || compareFxAmounts(quote.repaid_gold, '0') === 1)" class="fx-preview" aria-label="交易拆分">
                <template v-if="crossesPosition">
                  <div v-for="(leg, index) in quote.legs" :key="index" class="fx-preview-row">
                    <span>{{ leg.purpose === 'short_cover' ? '回补' : leg.purpose === 'short_open' ? '新增空头' : leg.side === 'sell' ? '卖出多头' : '新增多头' }}</span>
                    <strong>{{ displayAmount(leg.side === 'sell' ? leg.input_amount : leg.output_amount) }} {{ currencyName }}</strong>
                  </div>
                </template>
                <div v-if="compareFxAmounts(quote.repaid_gold, '0') === 1" class="fx-preview-row"><span>自动还款</span><strong>{{ displayAmount(quote.repaid_gold, 2) }} 金圆券</strong></div>
              </div>
              <div v-if="amountValid" class="fx-order-margin"><div class="fx-preview-row"><span>预计成交后保证金率</span><strong>{{ postTradeMarginDisplay }}</strong></div><small>维持门槛 {{ maintenancePercent }}，低于门槛将被强平</small></div>
              <div class="fx-submit-state" role="status">
                <span v-if="tradeError || tradeBlockReason" class="fx-error">{{ tradeError || tradeBlockReason }}</span
                ><span v-else-if="submitting" class="fx-hint">提交中…</span
                ><span v-else-if="quoting" class="fx-hint">报价中…</span
                ><span v-else-if="amount && !amountValid" class="fx-hint"
                  >请输入正数，最多 6 位小数。</span
                ><span v-else-if="quoteExpired" class="fx-error">报价已过期，请重新报价。</span>
              </div>
              <div class="fx-actions">
                <button
                  class="fx-submit"
                  :disabled="
                    submitting ||
                    pendingRisk ||
                    !amountValid ||
                    !!tradeBlockReason ||
                    quoting ||
                    !quote ||
                    quoteExpired
                  "
                  @click="submitTrade"
                >
                  <FxSubmitSummary v-if="spotSubmitSummary" v-bind="spotSubmitSummary" :action="spotSubmitLabel" :currency="currencyName" />
                  <template v-else>{{ spotSubmitLabel }}</template></button
                ><button
                  class="fx-text-button"
                  :disabled="quoting || submitting || !tradable || !amountValid || pendingRisk"
                  @click="fetchQuote"
                >
                  重新报价
                </button>
              </div>
              <details class="fx-details fx-order-details">
                <summary>成交保护与风险</summary>
                <label class="fx-field fx-field--inline">
                  <span>报价变动容忍度</span>
                  <select v-model.number="slippageBps" class="fx-input" :disabled="!tradable || submitting">
                    <option :value="50">0.5%</option>
                    <option :value="100">1%</option>
                    <option :value="200">2%</option>
                    <option :value="500">5%</option>
                  </select>
                </label>
                <div class="fx-preview-row">
                  <span>{{ side === 'buy' ? '最低买入数量' : '最低卖出所得' }}</span>
                  <strong>{{ minOutDisplay }} {{ outputCurrency }}</strong>
                </div>
                <p class="fx-protection-note">超出成交限额时，订单不会执行。</p>
              </details>
            </div>

            <div v-show="tradeMode === 'short'" class="fx-trade-body">
              <p v-if="!authStore.user?.id" class="fx-error" role="alert">
                请先登录后使用空头交易。
              </p>
              <div v-if="shortAction === 'open'" class="fx-short-size">
                <div class="fx-preview-row fx-short-quantity">
                  <span>借币数量</span
                  ><strong
                    >{{ shortAmount ? displayAmount(shortAmount) : '—' }}
                    {{ currencyName }}</strong
                  >
                </div>
                <p v-if="hasSpotHolding" class="fx-error">请先平掉当前多头，再做空这个币种。</p>
              </div>
              <div v-else>
                <label class="fx-field"
                  ><span
                    >回补数量 <small>{{ currencyName }}</small></span
                  ><input
                    v-model="shortAmount"
                    class="fx-input fx-size-input"
                    inputmode="decimal"
                    autocomplete="off"
                    :disabled="submitting || pendingRisk || coverAll"
                    :placeholder="coverAll ? '全部欠币' : '0'"
                /></label>
                <div class="fx-sell-shortcuts">
                  <button
                    v-for="percent in portions"
                    :key="percent"
                    class="btn-secondary"
                    :disabled="percent === 100 ? !canFillCoverAll : !canFillCover"
                    :aria-pressed="percent === 100 && coverAll"
                    @click="fillCoverPortion(percent)"
                  >
                    {{ percent === 100 ? '全部回补' : `${percent}%` }}
                  </button>
                </div>
                <p v-if="coverAll" class="fx-field-note">已选全部回补，包含成交时利息。</p>
                <p v-if="shortFailed" class="fx-error">
                  持仓更新失败，可选择全部平仓获取最新报价。
                </p>
              </div>
              <div v-if="shortValid" class="fx-quote-box" aria-label="空头交易报价">
                <div class="fx-short-funding">
                  <div class="fx-preview-row">
                    <span>{{ shortAction === 'open' ? '预计锁定所得' : '预计回补成本' }}</span
                    ><strong
                      >{{
                        shortQuote
                          ? displayAmount(
                              shortAction === 'open'
                                ? shortQuote.restricted_gold_delta
                                : shortQuote.input_amount,
                            )
                          : '—'
                      }}
                      金圆券</strong
                    >
                  </div>
                  <p>
                    {{
                      shortAction === 'open'
                        ? '所得锁定用于回补。'
                        : '优先使用本仓锁金。'
                    }}
                  </p>
                </div>
                <FxExecutionPrice
                  :average="shortAveragePrice"
                  :reference="shortReferencePrice"
                  :currency="currencyName"
                />
                <div class="fx-preview fx-order-summary fx-quote-meta">
                  <div class="fx-preview-row">
                    <span>手续费（已含）</span
                    ><strong
                      >{{ shortQuote ? displayAmount(shortQuote.fee_amount) : '—' }}
                      {{
                        shortQuote?.fee_currency === 'foreign'
                          ? currencyName
                          : shortQuote?.fee_currency === 'gold'
                            ? '金圆券'
                            : shortAction === 'open'
                              ? currencyName
                              : '金圆券'
                      }}</strong
                    >
                  </div>

                </div>
                <FxClosePnl
                  v-if="shortAction === 'cover'"
                  :pnl="shortClosePreview?.pnl ?? null"
                  :note="
                    coverAll
                      ? '含报价手续费及报价时的全部欠币利息。'
                      : '按最近含息欠币分摊开空所得；含报价手续费。'
                  "
                  :unavailable="shortCloseUnavailable"
                />
              </div>
              <div v-if="shortValid" class="fx-order-margin"><div class="fx-preview-row"><span>预计成交后保证金率</span><strong>{{ postTradeMarginDisplay }}</strong></div><small>维持门槛 {{ maintenancePercent }}，低于门槛将被强平</small></div>
              <div class="fx-submit-state" role="status">
                <span v-if="shortNotice" class="fx-error">{{ shortNotice }}</span>
                <span v-else-if="shortQuote?.risk_blocked_reason" class="fx-hint">{{ shortBlockMessage(shortQuote.risk_blocked_reason) }}</span>
                <span v-else-if="shortQuoting" class="fx-hint">报价中…</span>
                <span v-else-if="shortExpired" class="fx-error">报价已过期，请重新报价。</span>
                <span v-else-if="shortAmount && !shortValid" class="fx-hint">请输入正数，最多 6 位小数。</span>
              </div>
              <div class="fx-actions">
                <button class="fx-submit" :disabled="!shortCanSubmit" @click="submitShort">
                  <FxSubmitSummary v-if="shortSubmitSummary" v-bind="shortSubmitSummary" :action="shortSubmitLabel" :currency="currencyName" />
                  <template v-else>{{ shortSubmitLabel }}</template></button
                ><button
                  class="fx-text-button"
                  :disabled="
                    !shortValid || shortQuoting || submitting || pendingRisk || shortStorageBlocked
                  "
                  @click="fetchShortQuote"
                >
                  重新报价
                </button>
              </div>
              <details class="fx-details fx-order-details">
                <summary>成交保护与风险</summary>
                <label class="fx-field fx-field--inline">
                  <span>报价变动容忍度</span>
                  <select v-model.number="slippageBps" class="fx-input" :disabled="submitting">
                    <option :value="50">0.5%</option>
                    <option :value="100">1%</option>
                    <option :value="200">2%</option>
                    <option :value="500">5%</option>
                  </select>
                </label>
                <div class="fx-preview-row">
                  <span>{{ shortAction === 'open' ? '最低卖出所得' : '最高回补支出' }}</span>
                  <strong>{{ displayAmount(shortLimit) }} 金圆券</strong>
                </div>
                <p class="fx-protection-note">超出成交限额时，订单不会执行。</p>
              </details>
            </div>
          </div>
        </aside>
      </div>

      <section v-if="netPending" class="fx-receipt" role="status">
        <p>有一笔交易结果待确认，请恢复原订单；期间不能提交新交易。</p>
        <button class="btn-secondary" :disabled="submitting" @click="retryNet">恢复原订单</button>
      </section>
      <section v-if="receipt" class="fx-receipt" role="status" aria-live="polite">
        <h2>{{ fxNetActionLabel(receipt.trade.legs) }}已成交：{{ receipt.currency }}</h2>
        <p>
          新增借款 {{ displayAmount(receipt.trade.borrow_amount, 2) }} 金圆券，成交后{{
            receipt.trade.final_direction === 'long'
              ? '持有多头'
              : receipt.trade.final_direction === 'short'
                ? '保留空头'
                : '已平仓'
          }}。
        </p>
        <p v-for="(leg, index) in receipt.trade.legs" :key="index">
          {{ fxNetActionLabel([leg]) }}
          {{ displayAmount(leg.side === 'sell' ? leg.input_amount : leg.output_amount) }}
          {{ receipt.currency }}。
        </p>
        <p>
          实际投入 {{ displayAmount(receipt.trade.input_amount) }}
          {{ receipt.trade.side === 'buy' ? '金圆券' : receipt.currency }}，{{
            receipt.trade.side === 'buy' ? '实际获得外币' : '实际卖出所得'
          }}
          {{ displayAmount(receipt.trade.output_amount) }}
          {{ receipt.trade.side === 'buy' ? receipt.currency : '金圆券' }}。
        </p>
        <details class="fx-details">
          <summary>成交明细</summary>
          <p>
            手续费 {{ displayAmount(receipt.trade.fee_amount) }}
            {{ receipt.trade.side === 'buy' ? '金圆券' : receipt.currency }}（已含），成交均价
            {{ formatFxAmount(fxGoldPerForeign(receipt.trade), 4) }} 金圆券 / {{ receipt.currency }}。
          </p>
          <p v-if="receipt.trade.side === 'sell'">
            自动还款 {{ displayAmount(receipt.trade.repaid_gold, 2) }} 金圆券，现金变化
            {{ displayAmount(receipt.trade.cash_delta, 2) }} 金圆券，锁金变化
            {{ displayAmount(receipt.trade.restricted_gold_delta, 2) }} 金圆券。
          </p>
        </details>
      </section>
      <section v-if="shortReceipt" class="fx-receipt" role="status" aria-live="polite">
        <h2>
          {{
            shortReceipt.purpose === 'short_open'
              ? '做空已成交，所得锁定'
              : '空头平仓已成交，外币已归还'
          }}
        </h2>
        <p>
          实际投入 {{ displayAmount(shortReceipt.input_amount) }}
          {{ shortReceipt.side === 'buy' ? '金圆券' : shortReceiptCurrency }}，产出
          {{ displayAmount(shortReceipt.output_amount) }}
          {{ shortReceipt.side === 'buy' ? shortReceiptCurrency : '金圆券' }}。手续费
          {{ displayAmount(shortReceipt.fee_amount) }}
          {{ shortReceipt.side === 'buy' ? '金圆券' : shortReceiptCurrency }}（已含）。
        </p>
      </section>

      <div class="fx-lower">
        <section class="fx-block fx-trades-block">
          <div class="fx-panel-head">
            <h2>我的成交记录</h2>
            <button class="fx-text-button" :disabled="tradesLoading" @click="loadTrades">
              {{ tradesLoading ? '刷新中…' : '刷新' }}
            </button>
          </div>
          <p v-if="tradesFailed" class="fx-error" role="alert">
            成交记录更新失败，显示上次记录，请刷新重试。
          </p>
          <p v-if="tradesLoading" class="fx-hint">正在读取成交记录…</p>
          <div class="table-wrap">
            <table class="fx-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>操作</th>
                  <th>投入</th>
                  <th>产出</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="t in groupedTrades" :key="t.id">
                  <td>{{ formatTime(t.created_at) }}</td>
                  <td>
                    {{
                      t.actionLabel ??
                      (t.purpose === 'borrow_buy'
                        ? '借款做多'
                        : t.purpose === 'short_open'
                          ? '做空'
                          : t.purpose === 'short_cover'
                            ? '空头平仓'
                            : t.side === 'buy'
                              ? '做多'
                              : '多头平仓')
                    }}
                    <details v-if="t.legs.length > 1" class="fx-details">
                      <summary>查看分段成交</summary>
                      <p v-for="leg in t.legs" :key="leg.id">
                        {{ fxNetActionLabel([leg]) }}：投入
                        {{ displayAmount(leg.input_amount) }}，产出
                        {{ displayAmount(leg.output_amount) }}。
                      </p>
                    </details>
                  </td>
                  <td>
                    {{ displayAmount(t.input_amount) }}
                    <small v-if="t.purpose === 'borrow_buy'"
                      >（借款 {{ displayAmount(t.borrow_amount) }} 金圆券）</small
                    >
                    {{ t.side === 'buy' ? '金圆券' : currencyName }}
                  </td>
                  <td>
                    {{ displayAmount(t.output_amount) }}
                    {{ t.side === 'buy' ? currencyName : '金圆券' }}
                  </td>
                </tr>
                <tr v-if="!tradesLoading && !tradesFailed && trades?.length === 0">
                  <td colspan="4" class="fx-empty">暂无成交</td>
                </tr>
              </tbody>
            </table>
          </div>
          <details class="fx-details">
            <summary>费用与记录时间</summary>
            <p>
              最近 50 笔当前币种成交，手续费已计入投入。{{
                tradesUpdatedAt ? `记录读取于 ${tradesUpdatedAt}。` : ''
              }}
            </p>
            <div v-for="t in groupedTrades" :key="t.id" class="fx-preview-row">
              <span>{{ formatTime(t.created_at) }} 手续费</span
              ><strong
                >{{ displayAmount(t.fee_amount) }}
                {{ t.side === 'buy' ? '金圆券' : currencyName }}；均价
                {{ formatFxAmount(fxGoldPerForeign(t), 4) }}</strong
              >
            </div>
          </details>
        </section>
      </div>
      <details class="fx-account-details fx-details">
        <summary>账户快照与借款规则</summary>
        <div class="fx-preview-row">
          <span>全部外币账面市值</span><strong>{{ displayAmount(summary?.fx_mtm) }} 金圆券</strong>
        </div>
        <div class="fx-preview-row">
          <span>全部外币浮动盈亏</span
          ><strong
            :class="(compareFxAmounts(summary?.fx_unrealized_pnl, '0') ?? 0) >= 0 ? 'up' : 'down'"
            >{{ displayAmount(summary?.fx_unrealized_pnl) }} 金圆券</strong
          >
        </div>
        <p>{{ summaryUpdatedAt ? `账户读取于 ${summaryUpdatedAt}。` : '等待账户数据。' }}</p>
        <p v-if="summary">
          FX
          持仓按可执行卖出报价计入清算估值；借款、做多与做空均需通过账户风控检查。卖出所得优先还款。
        </p>

        <p>外币不能直接用于预测市场、兑换商品或偿还金圆券借款。</p>
        <router-link to="/loan">查看借款与还款</router-link>
      </details>
    </template>
    <FxBorrowConfirmation
      :details="borrowConfirmationPending?.details ?? null"
      @cancel="borrowConfirmation.cancel"
      @confirm="borrowConfirmation.confirm"
    />
    <MobileTradeDock
      :visible="!loading && !error && !!activePair && !tradePanelVisible"
      :label="currencyName"
      :side="side"
      :disabled="submitting || pendingRisk"
      :show-short="hasSpotHolding || hasShortHolding"
      :short-active="tradeMode === 'short'"
      :show-sell="true"
      buy-label="买入"
      sell-label="卖出"
      short-label="平仓"
      @select="openFxTrade"
      @short="openShortTrade"
    />
  </div>
</template>

<style scoped>
.fx-page {
  max-width: none;
  color: #000;
  font-size: 13px;
  padding: 4px;
}
.fx-page :is(button, input, select) {
  font: inherit;
}
.fx-page :is(button, input, select, summary, a):focus-visible {
  outline: 2px solid #000;
  outline-offset: 3px;
}
.fx-page :is(strong, b) {
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.fx-page button {
  cursor: pointer;
}
.fx-page button:disabled {
  cursor: not-allowed;
  opacity: 0.3;
}
.fx-page a {
  color: #000;
  text-decoration: underline;
  text-underline-offset: 3px;
}
.up {
  color: var(--color-up);
}
.down,
.fx-error,
.fx-state-error {
  color: var(--color-down);
}
.fx-error,
.fx-hint {
  font-size: 12px;
  line-height: 1.6;
}
.fx-hint {
  color: #777;
}
.fx-text-button {
  border: 0;
  padding: 3px 0;
  background: none;
  color: inherit;
  font-size: 12px !important;
  text-decoration: underline;
  text-underline-offset: 3px;
}
.btn-secondary,
.fx-black-button {
  border: 2px solid #000;
  background: #fff;
  color: #000;
  font-size: 12px;
  padding: 6px 12px;
  min-height: var(--trade-control-height);
  font-weight: 700 !important;
}
.fx-black-button {
  background: #000;
  color: #fff;
}
.fx-topbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 18px;
  padding: 10px 12px;
  margin-bottom: 8px;
  background: #000;
  color: #fff;
  border: 2px solid #000;
}
.fx-topbar .fx-title,
.fx-topbar .fx-price,
.fx-topbar .fx-market-volume > strong {
  color: #fff;
}
.fx-topbar .fx-title small,
.fx-topbar .fx-topbar-label,
.fx-topbar .fx-market-volume > span,
.fx-topbar .fx-connection {
  color: rgba(255, 255, 255, 0.6);
}
.fx-topbar .fx-price.up {
  color: var(--color-up-strong);
}
.fx-topbar .fx-price.down {
  color: var(--color-down-strong);
}
.fx-topbar-id {
  display: flex;
  align-items: center;
  gap: 12px;
}
.fx-title {
  font-size: 19px;
  font-weight: 800;
  margin: 0;
}
.fx-title small {
  font-size: 11px;
  font-weight: 500;
  color: #777;
  margin-left: 8px;
}
.fx-pair-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.fx-pair-select {
  border: 1px solid #bbb;
  background: #fff;
  padding: 5px 8px;
  font-size: 13px;
  font-weight: 650 !important;
  max-width: 100%;
}
.fx-status {
  background: #f5f5f5;
  color: #555;
  padding: 3px 6px;
  font-size: 10px;
}
.fx-topbar-price {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.fx-topbar-label {
  color: #777;
  font-size: 11px;
}
.fx-price {
  font-size: 27px;
  font-weight: 750 !important;
  line-height: 1.2;
}
.fx-price.up {
  color: var(--color-up);
}
.fx-price.down {
  color: var(--color-down);
}
.fx-market-volume {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-left: auto;
}
.fx-market-volume > span {
  color: #777;
  font-size: 11px;
}
.fx-market-volume > strong {
  font-size: 13px;
}
.fx-market-volume small {
  font-size: 10px;
  color: #777;
}
.fx-connection {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #777;
  font-size: 11px;
}
.fx-stream-dot {
  width: 6px;
  height: 6px;
  border: 1px solid #777;
  background: #fff;
}
.fx-stream-dot.on {
  background: var(--color-up);
  border-color: var(--color-up);
}
.fx-freshness-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 14px;
  font-size: 11px;
  color: #888;
  padding-bottom: 8px;
}
.fx-freshness-bar > button {
  margin-left: auto;
}
.fx-account-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 22px;
  background: #000;
  color: #fff;
  border: 2px solid #000;
  padding: 0;
  margin-bottom: 0;
  font-size: 12px;
}
.fx-account-bar > span {
  font-weight: 650;
}
.fx-account-bar > div {
  display: flex;
  flex-direction: column;
  gap: 1px;
  color: rgba(255, 255, 255, 0.5);
  font-size: 10px;
}
.fx-account-bar > div > strong {
  color: #fff;
  margin-left: 0;
  font-size: 12px;
}
.fx-account-bar > div:first-of-type > strong {
  color: #aaffaa;
}
.fx-account-bar strong {
  color: #000;
  margin-left: 7px;
}
.fx-account-bar > div > strong.fx-error {
  color: var(--color-down-strong);
}
.fx-account-bar > .fx-account-margin > strong {
  font-size: 22px;
  line-height: 1.15;
  font-variant-numeric: tabular-nums;
}
.fx-account-bar > .fx-account-margin--warning > strong,
.fx-account-bar > .fx-account-margin--warning > span {
  color: #ffb3a7;
}
.fx-account-bar > .fx-account-maintenance > strong {
  font-size: 16px;
  font-variant-numeric: tabular-nums;
}
.fx-account-bar > div > span {
  font-size: 10px;
  color: rgba(255, 255, 255, 0.65);
}
.fx-protection-note {
  color: #777;
  font-size: 11px;
  line-height: 1.6;
}
.fx-account-bar small {
  color: #999;
  margin-left: auto;
  font-size: 10px;
}
.fx-state {
  border: 1px solid #000;
  padding: 24px;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}
.fx-notice {
  padding: 10px 12px;
  background: #f5f5f5;
  margin: 0 0 16px;
  font-size: 12px;
}
.fx-pending-short {
  border: 1px solid #000;
  padding: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  margin-bottom: 18px;
  background: #fafafa;
}
.fx-pending-short p {
  margin-top: 5px;
  color: #666;
  font-size: 12px;
}
.fx-pending-short > button {
  flex-shrink: 0;
}
.fx-workbench {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 380px;
  gap: 24px;
  min-height: max(440px, var(--fx-workbench-height, calc(100dvh - 240px)));
  align-items: stretch;
}
.fx-market-column {
  min-width: 0;
  min-height: 0;
  height: max(440px, var(--fx-workbench-height, calc(100dvh - 240px)));
  align-self: start;
  display: flex;
  flex-direction: column;
}
.fx-chart-panel {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid #000;
  padding: var(--trade-panel-padding);
  box-sizing: border-box;
}
.fx-panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}
.fx-panel-head h2 {
  font-size: 14px;
  font-weight: 750;
  margin: 0;
}
.fx-intervals {
  display: flex;
  gap: 4px;
}
.fx-interval {
  border: 0;
  background: #fff;
  color: #777;
  padding: 5px 9px;
  font-size: 11px !important;
}
.fx-interval.active {
  background: #000;
  color: #fff;
}
.fx-chart-body {
  position: relative;
  flex: 1;
  min-height: 0;
  min-width: 0;
}
.fx-chart-order-entry {
  position: absolute;
  top: 8px;
  left: 8px;
  z-index: 2;
}
.fx-chart-order-entry :deep(.manage-orders) {
  color: #000;
  background: #fff;
  white-space: nowrap;
  font-size: 11px;
  min-height: 32px;
  padding: 4px 8px;
}
.fx-chart-footer {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  color: #888;
  font-size: 11px;
  padding-top: 10px;
}

.fx-trade-panel {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: visible;
  box-sizing: border-box;
  border: 2px solid #000;
  padding: var(--trade-panel-padding);
  background: #fff;
}
.fx-order-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  gap: 12px;
}
.fx-trade-anchor {
  position: absolute;
  top: 0;
  left: 0;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  scroll-margin-top: 85px;
}
.fx-action-picker {
  flex-shrink: 0;
}
.fx-panel-scroll {
  flex: 1;
  min-height: 0;
}
.fx-order-margin { display: grid; gap: 3px; margin: 6px 0; }
.fx-trade-body .fx-preview-row { font-size: 11px; padding-bottom: 3px; }
.fx-order-margin .fx-preview-row { padding-bottom: 0; }
.fx-order-margin strong { font-size: 18px; font-weight: 800; }
.fx-order-margin small { font-size: 10px; color: #777; }
.fx-current-position {
  display: grid;
  gap: 6px;
  margin-bottom: 8px;
}
.fx-position-details {
  font-size: 10px;
  color: #777;
  margin-bottom: 10px;
}
.fx-position-details > summary {
  cursor: pointer;
}
.fx-no-position {
  border: 1px solid #ddd;
  background: #fafafa;
  padding: 8px;
  font-size: 11px;
  color: #777;
}
.fx-trade-heading {
  margin: 0;
  font-size: 14px;
  font-weight: 700;
  scroll-margin-top: 85px;
}
.fx-order-heading > span {
  color: #777;
  font-size: 12px;
}
.fx-short-unit {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #777;
  font-size: 11px;
}
.fx-short-unit select {
  background: #fff;
  border: 2px solid #000;
  padding: 3px;
  font: inherit;
}
.fx-direction-picker {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0;
  margin-bottom: var(--trade-panel-gap);
  border: 2px solid #000;
}
.fx-direction-picker > button {
  display: grid;
  grid-template-columns: 20px 1fr;
  column-gap: 5px;
  text-align: left;
  align-items: center;
  border: 0;
  padding: 4px 8px;
  min-height: var(--trade-action-height);
  background: #fff;
  color: #000;
}
.fx-direction-picker > button:nth-child(odd) {
  border-right: 2px solid #000;
}
.fx-direction-picker > button:nth-child(-n + 2) {
  border-bottom: 2px solid #000;
}
.fx-direction-picker strong {
  font-size: 13px;
  white-space: nowrap;
}
.fx-direction-picker small {
  grid-column: 2;
  color: #888;
  font-size: 10px;
  margin-top: 2px;
}
.fx-direction-picker button > span {
  grid-row: auto;
  font-size: 18px;
  font-weight: 750;
  align-self: center;
  line-height: 1.3;
}
.fx-direction-long.active {
  background: #000;
  color: #fff;
}
.fx-direction-short.active {
  background: var(--color-down);
  color: #fff;
}
.fx-action-picker .fx-close-choice {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  padding: 4px 8px;
  justify-content: center;
}
.fx-action-picker .fx-close-choice strong {
  font-size: 13px;
}
.fx-close-choice.active {
  background: #000;
  color: #fff;
}
.fx-close-choice--sell.active {
  background: var(--color-down);
}
.fx-direction-help {
  color: #777;
  font-size: 11px;
  margin: 9px 0 20px;
}
.fx-close-context {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  margin: 0 0 12px;
  background: #f5f5f5;
  font-size: 12px;
}
.fx-field {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 9px;
}
.fx-field > span {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  font-weight: 650;
  gap: 8px;
}
.fx-field small {
  font-size: 11px;
  font-weight: 400;
  color: #777;
}
.fx-input {
  width: 100%;
  min-width: 0;
  border: 2px solid #000;
  padding: 5px 8px;
  background: #fff;
  color: #000;
}
.fx-size-input {
  font-size: 14px !important;
  font-weight: 700 !important;
  min-height: var(--trade-control-height);
}
.fx-size-input::placeholder {
  color: #aaa;
  font-size: 13px;
}
.fx-field--inline {
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  margin-top: 12px;
}
.fx-field--inline .fx-input {
  width: 84px;
  padding: 5px;
  font-size: 12px;
}
.fx-sell-shortcuts,
.fx-short-presets {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 6px;
  margin-bottom: 8px;
}
.fx-sell-shortcuts > button,
.fx-short-presets > button {
  min-width: 0;
  border-color: #000;
  padding: 5px 0;
  font-size: 11px !important;
  color: #000;
  min-height: var(--trade-control-height);
}
.fx-short-presets {
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin-top: 10px;
}
.fx-field-note {
  margin: 6px 0 15px;
  font-size: 11px;
  line-height: 1.6;
  color: #888;
}
.fx-size-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  font-size: 12px;
  gap: 8px;
}
.fx-size-label > label {
  font-weight: 650;
}
.fx-size-label select {
  border: 0;
  border-bottom: 1px solid #ccc;
  padding: 3px;
  background: #fff;
  color: #777;
  font-size: 11px;
  max-width: 120px;
}
.fx-cover-all {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 10px 0 16px;
  color: #777;
  font-size: 11px;
}
.fx-funding-preview {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 0 0 6px;
  border-bottom: 1px solid #000;
  margin: 0 0 6px;
}
.fx-funding-preview > div {
  display: flex;
  flex: 1;
  align-items: baseline;
  justify-content: space-between;
  gap: 5px;
  min-width: 0;
}
.fx-funding-preview span {
  color: #777;
  font-size: 10px;
}
.fx-funding-preview strong {
  font-size: 11px;
  overflow-wrap: anywhere;
}
.fx-funding-preview > b {
  color: #aaa;
  font-size: 12px;
}
.fx-credit-entry {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 11px;
  margin-bottom: 16px;
}
.fx-credit-entry > div {
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: #666;
}
.fx-credit-entry small {
  color: #888;
  font-size: 10px;
}
.fx-credit-entry a {
  flex-shrink: 0;
}
.fx-preview-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 10px;
  font-size: 12px;
  padding-bottom: 8px;
}
.fx-preview-row > span {
  color: #777;
}
.fx-preview-row > strong {
  text-align: right;
  overflow-wrap: anywhere;
  min-width: 0;
}
.fx-order-summary {
  padding-top: 0;
  margin: 0;
}
.fx-quote-box {
  padding: 8px 10px;
  border: 1px solid #000;
  background: #fff;
  margin-bottom: var(--trade-panel-gap);
}
.fx-order-summary .fx-preview-row {
  padding-bottom: 3px;
}
.fx-quote-meta {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 6px 12px;
}
.fx-quote-meta .fx-preview-row {
  gap: 5px;
  font-size: 10px;
}
.fx-short-funding {
  padding: 0 0 6px;
  border-bottom: 1px solid #000;
  margin: 0 0 6px;
}
.fx-short-quantity {
  padding-bottom: 0;
}
.fx-short-funding .fx-preview-row {
  padding-bottom: 3px;
}
.fx-short-funding p {
  font-size: 11px;
  color: #888;
  line-height: 1.6;
}
.fx-shared-risk {
  font-size: 11px;
  line-height: 1.6;
  color: #777;
  margin: 10px 0 15px;
}
.fx-trade-body > .fx-error,
.fx-trade-body > .fx-hint {
  margin-bottom: 10px;
}
.fx-submit-state {
  font-size: 11px;
  padding-bottom: 8px;
}
.fx-submit-state:empty {
  display: none;
}
.fx-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}
.fx-submit {
  display: block;
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
  background: #000;
  color: #fff;
  padding: 6px 10px;
  border: 2px solid #000;
  font-size: 14px !important;
  font-weight: 800 !important;
  min-height: var(--trade-action-height);
}
.fx-actions > .fx-text-button {
  flex-shrink: 0;
  font-size: 11px !important;
}
.fx-details {
  font-size: 11px;
  color: #777;
  margin-top: 12px;
}
.fx-details summary {
  cursor: pointer;
}
.fx-details p {
  margin-top: 8px;
  line-height: 1.7;
}
.fx-details .fx-preview-row {
  margin-top: 8px;
}
.fx-order-details {
  border-top: 1px solid #eee;
  padding-top: 8px;
}
.fx-details :deep(.credit-risk) {
  margin-top: 14px;
}
.fx-details :deep(.credit-risk-ratio) {
  font-size: 21px;
}
.fx-receipt {
  margin-top: 18px;
  padding: 15px;
  background: #f7f7f7;
  border: 1px solid #ddd;
  font-size: 12px;
}
.fx-receipt h2 {
  font-size: 14px;
  font-weight: 750;
  margin-bottom: 8px;
}
.fx-lower {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 28px;
  margin-top: 28px;
  border-top: 1px solid #ddd;
  padding-top: 22px;
}
.fx-block {
  min-width: 0;
}
.fx-empty {
  font-size: 12px;
  padding: 15px 0;
  color: #888;
  text-align: left;
}
.table-wrap {
  overflow-x: auto;
}
.fx-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
  text-align: left;
}
.fx-table :is(th, td) {
  border-bottom: 1px solid #eee;
  padding: 8px;
  white-space: nowrap;
}
.fx-table th {
  color: #777;
  font-weight: 500;
  background: #f7f7f7;
}
.fx-account-details {
  border-top: 1px solid #eee;
  padding-top: 14px;
  margin-top: 25px;
}
@media (max-width: 1150px) {
  .fx-workbench {
    grid-template-columns: minmax(0, 1fr) 330px;
    gap: 20px;
  }
  .fx-trade-panel {
    padding: var(--trade-panel-padding);
  }
  .fx-account-bar {
    gap: 10px 16px;
  }
  .fx-account-bar small {
    display: none;
  }
  .fx-position-bottom {
    flex-wrap: wrap;
  }
  .fx-position-bottom > span {
    max-width: 100%;
  }
  .fx-position-bottom > div {
    margin-left: auto;
  }
}
@media (max-width: 950px) {
  .fx-workbench {
    display: flex;
    flex-direction: column;
    gap: 24px;
    height: auto;
    min-height: 0;
  }
  .fx-trade-panel {
    width: 100%;
    height: auto;
    overflow: visible;
  }
  .fx-panel-scroll {
    overflow: visible;
    scrollbar-gutter: auto;
    padding-right: 0;
  }
  .fx-chart-panel {
    height: auto;
  }
  .fx-market-column {
    width: 100%;
    height: auto;
    position: static;
  }
  .fx-market-volume {
    display: none;
  }
  .fx-connection {
    margin-left: auto;
  }
  .fx-chart-body {
    flex: none;
    height: 340px;
  }
  .fx-lower {
    grid-template-columns: 1fr;
    gap: 24px;
  }
  .fx-account-bar {
    font-size: 11px;
  }
  .fx-page {
    padding-bottom: calc(88px + env(safe-area-inset-bottom, 0px));
  }
}
@media (max-width: 640px) {
  .fx-page {
    padding: 0 0 calc(88px + env(safe-area-inset-bottom, 0px));
  }
  .fx-topbar {
    gap: 12px;
    padding-bottom: 13px;
  }
  .fx-title {
    font-size: 13px;
  }
  .fx-title small {
    font-size: 10px;
  }
  .fx-topbar-price {
    margin-left: auto;
    text-align: right;
  }
  .fx-price {
    font-size: 27px;
  }
  .fx-topbar-label {
    font-size: 10px;
  }
  .fx-pair-select {
    max-width: 180px;
    font-size: 12px;
  }
  .fx-connection {
    font-size: 10px;
    margin-left: 0;
  }
  .fx-account-bar {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    width: 100%;
    box-sizing: border-box;
    gap: 12px 18px;
    padding: 10px;
    margin-bottom: 18px;
  }
  .fx-account-bar > span {
    display: none;
  }
  .fx-account-bar strong {
    margin-left: 4px;
  }
  .fx-holdings-overview {
    flex-basis: 100%;
    margin-left: 0;
  }
  .fx-trade-panel {
    padding: var(--trade-panel-padding);
  }
  .fx-direction-picker > button {
    padding: 10px 12px;
    min-height: 44px;
  }
  .fx-sell-shortcuts > button,
  .fx-short-presets > button {
    min-height: 38px;
  }
  .fx-input {
    min-height: 44px;
    font-size: 16px;
  }
  .fx-size-input {
    font-size: 16px !important;
  }
  .fx-submit {
    min-height: 44px;
  }
  .fx-chart-body {
    height: 270px;
  }
  .fx-interval {
    min-height: 38px;
    padding: 5px 8px;
  }
  .fx-chart-footer > span:first-child {
    display: none;
  }
  .fx-panel-head > .fx-hint {
    display: none;
  }
  .fx-position-bottom button {
    min-height: 40px;
  }
  .fx-pending-short {
    flex-wrap: wrap;
  }
  .fx-freshness-bar .fx-text-button {
    min-height: 32px;
  }
}
</style>
