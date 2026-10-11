<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { useRoute } from 'vue-router'
import {
  FxPendingShortOrder,
  computeMaxGoldIn,
  subtractFxAmounts,
  compareFxAmounts,
  computeMinOut,
  divideFxAmount,
  formatFxAmount,
  formatFxPrice,
  multiplyFxAmount,
  fxApi,
  FxOrderSubmitter,
  fxOrderSignature,
  FxStream,
  isConflictError,
  mapFxError,
} from '@/api/fx'
import type { FxPendingShortRequest } from '@/api/fx'
import { userApi } from '@/api/user'
import { useAuthStore } from '@/stores/auth'
import type { UserSummary } from '@/types/user'
import { useFxResource } from '@/composables/useFxResource'
import { useFxShortQuote, type ShortQuoteOrder } from '@/composables/useFxShortQuote'
import { FxPendingBorrowBuyOrder } from '@/api/fxBorrowBuy'
import type { FxBorrowBuyRequest, FxBorrowBuyTrade } from '@/types/fx'
import FxBorrowBuyForm from '@/components/market/FxBorrowBuyForm.vue'
import FxOrderSize from '@/components/market/FxOrderSize.vue'
import FxExecutionPrice from '@/components/market/FxExecutionPrice.vue'
import FxPostTradeMargin from '@/components/market/FxPostTradeMargin.vue'
import FxClosePnl from '@/components/market/FxClosePnl.vue'
import {
  fxAvailableCash,
  fxBuyBlockReason,
  fxFundingPreview,
  fxGoldPerForeign,
  fxHoldingValue,
  fxPairAllowsSide,
  fxSellAllocation,
  fxShortRiskStatus,
  fxOrderPrincipal,
  fxOrderAmountLabel,
  fxShortGoldPerForeign,
  fxSpotClosePreview,
  fxShortClosePreview,
  fxHoldingAveragePrice,
} from '@/utils/fxPresentation'
import { resolveCreditRiskStatus } from '@/utils/creditRiskStatus'
import type {
  FxPersonalTrade,
  FxShortPosition,
  FxShortTrade,
  FxChartInterval,
  FxPairPublic,
  FxPublicEnvelope,
  FxQuote,
  FxSide,
  FxSnapshot,
  FxTradePublic,
  FxWalletPublic,
  FxHoldingPriceLine,
} from '@/types/fx'
import FxCandleChart from '@/components/chart/FxCandleChart.vue'
import MobileTradeDock from '@/components/market/MobileTradeDock.vue'
import ShortPositionPnl from '@/components/user/ShortPositionPnl.vue'
import CreditRiskStatus from '@/components/user/CreditRiskStatus.vue'
import { useMobileTradeEntry } from '@/composables/useMobileTradeEntry'

defineOptions({ name: 'FxPage' })

const msg = useMessage()
const route = useRoute()
const authStore = useAuthStore()

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
  updatedAt: walletUpdatedAt,
} = walletResource
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
const quoteExpired = computed(
  () => !!quoteUpdatedAt.value && now.value - quoteUpdatedAt.value >= 30000,
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

const tradeMode = ref<'spot' | 'short'>(
  route.query.action === 'cover' || route.query.action === 'open' ? 'short' : 'spot',
)
const side = ref<FxSide>('buy')
const { tradePanelRef, tradePanelVisible, openTrade } = useMobileTradeEntry(setSide)
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
const slippageBps = ref<number>(50)
const quote = ref<FxQuote | null>(null)
const quoting = ref(false)
const submitting = ref(false)
const tradeError = ref<string | null>(null)
const orderSubmitter = new FxOrderSubmitter()

const summaryResource = useFxResource<UserSummary>()
const {
  data: summary,
  loading: summaryLoading,
  failed: summaryFailed,
  updatedAt: summaryUpdatedAt,
} = summaryResource
const receipt = ref<{ trade: FxTradePublic; currency: string } | null>(null)
const quoteReferencePrice = ref<string | null>(null)

const activePair = computed(() =>
  snapshot.value?.pair.id === pairId.value
    ? snapshot.value.pair
    : (pairs.value.find((p) => p.id === pairId.value) ?? null),
)
const tradable = computed(() => fxPairAllowsSide(activePair.value, side.value))
const currencyName = computed(() => activePair.value?.currency_name ?? '外币')
/** 交易面板顶部按方向显示对应的有效买卖价 */
const sidePrice = computed(() =>
  side.value === 'buy' ? snapshot.value?.buy_price : snapshot.value?.sell_price,
)

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
  amount.value = computeMinOut(wallet.value.foreign_amount, (100 - percent) * 100)
}

async function prepareSpotSellAll(percent = 100) {
  if (!canSellSpotHolding.value) return
  setSide('sell')
  fillSellPortion(percent)
  await openTrade('sell')
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
const minOutDisplay = computed(() => (minOut.value ? formatFxAmount(minOut.value) : '—'))
const effectiveGoldPerForeign = computed(() => (quote.value ? fxGoldPerForeign(quote.value) : null))
const holdingValue = computed(() => fxHoldingValue(wallet.value, snapshot.value?.price))
const shortHoldingPnl = computed(() =>
  shortPosition.value?.reference_cover_cost == null
    ? null
    : subtractFxAmounts(
        shortPosition.value.proceeds_basis_gold,
        shortPosition.value.reference_cover_cost,
      ),
)
const spotPnlDirection = computed(() => {
  const direction = compareFxAmounts(holdingValue.value?.pnl, '0')
  return direction === 1 ? 'up' : direction === -1 ? 'down' : 'flat'
})
const outputCurrency = computed(() => (side.value === 'buy' ? currencyName.value : '金圆券'))
const inputCurrency = computed(() => (side.value === 'buy' ? '金圆券' : currencyName.value))
const sellAllocation = computed(() =>
  side.value === 'sell' && quote.value && !summaryFailed.value && !summaryLoading.value
    ? fxSellAllocation(quote.value.output_amount, summary.value)
    : null,
)
const spotClosePreview = computed(() =>
  !quoting.value &&
  !quoteExpired.value &&
  !walletLoading.value &&
  !walletFailed.value &&
  wallet.value?.pair_id === pairId.value &&
  quote.value?.pair_id === pairId.value
    ? fxSpotClosePreview(quote.value, wallet.value)
    : null,
)
const spotCloseUnavailable = computed(() => {
  if (quoteExpired.value) return '报价已过期，重新报价后显示盈亏。'
  if (walletLoading.value) return '正在更新持仓成本…'
  if (walletFailed.value) return '持仓成本更新失败，请重新加载。'
  if (!quote.value || quoting.value) return '等待本笔卖出报价。'
  return '持仓成本或卖出数量暂不可确认。'
})
const buyBlockReason = computed(() => {
  if (summaryLoading.value) return '正在刷新账户，请稍候'
  if (summaryFailed.value) return '账户信息刷新失败，请重新加载后买入'
  return fxBuyBlockReason(summary.value, amount.value.trim())
})
const tradeBlockReason = computed(() => {
  if (!tradable.value)
    return fxPairAllowsSide(activePair.value, 'sell')
      ? '当前币种只允许卖出，不能买入'
      : '当前币种未开放交易'
  if (!snapshot.value) return '正在读取行情，请稍候'
  if (side.value === 'buy') {
    if (compareFxAmounts(shortPosition.value?.pending_short_debt, '0') === 1)
      return '请先平掉当前空头，再做多这个币种'
    return buyBlockReason.value
  }
  if (walletLoading.value) return '正在读取持仓，请稍候'
  if (walletFailed.value || !wallet.value) return '持仓读取失败，请重新加载后卖出'
  if (compareFxAmounts(wallet.value.foreign_amount, '0') !== 1) return '暂无可卖出的持仓'
  if (compareFxAmounts(amount.value.trim(), wallet.value.foreign_amount) === 1)
    return '卖出数量超过持仓，请减少数量'
  return ''
})
const walletAvgCost = computed(() => {
  const w = wallet.value
  if (!w) return null
  return divideFxAmount(w.cost_basis, w.foreign_amount, 12)
})
const holdingAuthGeneration = ref(0)
const walletHoldingGeneration = ref(-1)
const shortHoldingGeneration = ref(-1)
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
const borrowing = computed(
  () =>
    side.value === 'buy' &&
    tradeMode.value === 'spot' &&
    compareFxAmounts(fundingPreview.value?.shortfall, '0') === 1,
)
const borrowSubmitter = new FxPendingBorrowBuyOrder(authStore.user?.id ?? null)
const borrowPending = ref(borrowSubmitter.pending)
const borrowUnreadable = ref(borrowSubmitter.unreadable)
const borrowUnresolved = ref(borrowSubmitter.hasUnresolved)
const pendingRisk = computed(() => !!pendingShort.value || borrowUnresolved.value)
const borrowError = ref('')
const borrowReceipt = ref<{ trade: FxBorrowBuyTrade; currency: string } | null>(null)
let borrowGeneration = 0
if (pendingShort.value) tradeMode.value = 'short'
const shortStorageBlocked = ref(shortSubmitter.unreadable || !authStore.user?.id)
watch(
  () => authStore.user?.id,
  (id) => {
    holdingAuthGeneration.value++
    borrowGeneration++
    if (borrowSubmitter.busy) submitting.value = false
    borrowSubmitter.setUser(id ?? null)
    syncBorrowState()
    borrowReceipt.value = null
    borrowError.value = ''
    amount.value = ''
    shortSubmitter.setUser(id ?? null)
    pendingShort.value = shortSubmitter.pending
    shortStorageBlocked.value = shortSubmitter.unreadable || !id
  },
  { immediate: true },
)
let shortReadGeneration = 0
const shortValid = computed(
  () =>
    (shortAction.value === 'cover' && coverAll.value) ||
    (/^\d+(\.\d{0,6})?$/.test(shortAmount.value.trim()) &&
      compareFxAmounts(shortAmount.value.trim(), '0') === 1),
)
const shortQuoteOrder = computed<ShortQuoteOrder | null>(() => {
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
const {
  quote: shortQuote,
  loading: shortQuoting,
  error: shortQuoteError,
  refresh: fetchShortQuote,
  quotedReferencePrice: shortReferencePrice,
} = useFxShortQuote(shortQuoteOrder, () =>
  !snapshotFailed.value && !snapshotLoading.value && snapshot.value?.pair.id === pairId.value
    ? snapshot.value.price
    : null,
)
const shortAveragePrice = computed(() =>
  shortQuote.value ? fxShortGoldPerForeign(shortQuote.value) : null,
)
const canFillShortOpen = computed(
  () =>
    !submitting.value &&
    !pendingRisk.value &&
    !shortStorageBlocked.value &&
    !snapshotLoading.value &&
    !snapshotFailed.value &&
    snapshot.value?.pair.id === pairId.value,
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
    insufficient_cash: '本仓锁定所得与可用现金不足，请减少回补数量或补充现金。',
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
  const quantity = divideFxAmount(gold, snapshot.value?.price, 6)
  if (quantity == null || compareFxAmounts(quantity, '0') !== 1) {
    return
  }
  shortAmount.value = quantity
}
function setShortSizeUnit(unit: 'gold' | 'foreign') {
  if (submitting.value || pendingRisk.value) return
  if (unit === 'gold')
    shortGoldAmount.value = snapshot.value
      ? (multiplyFxAmount(shortAmount.value, snapshot.value.price) ?? '')
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
const shortPostMargin = computed(() => {
  const q = shortQuote.value
  if (q?.estimated_equity == null || q.estimated_risk_basis == null) return null
  const ratio = divideFxAmount(q.estimated_equity, q.estimated_risk_basis, 12)
  return ratio == null ? null : Number(ratio)
})

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
  if (pendingRisk.value) return '待确认订单结果'
  if (!amountValid.value) return amount.value ? '请输入有效金额' : '请输入交易金额'
  if (tradeBlockReason.value) return '暂不能成交'
  if (quoting.value) return '正在更新报价…'
  if (quoteExpired.value) return '报价已过期'
  if (!quote.value) return tradeError.value ? '报价失败，请重试' : '等待报价…'
  if (side.value === 'sell' && spotClosePreview.value?.full)
    return `卖出全部 ${fxOrderAmountLabel(quote.value.input_amount)} ${currencyName.value}`
  return `${side.value === 'buy' ? '买入' : '卖出'} ${fxOrderAmountLabel(amount.value)} ${inputCurrency.value}`
})
const shortSubmitLabel = computed(() => {
  if (submitting.value) return '提交中…'
  if (pendingRisk.value) return '待确认订单结果'
  if (shortStorageBlocked.value) return '暂不能提交订单'
  if (!shortValid.value) return shortAmount.value ? '请输入有效数量' : '请输入交易金额'
  if (shortQuoting.value) return '正在更新报价…'
  if (shortExpired.value) return '报价已过期'
  if (shortQuoteError.value) return '报价失败，请重试'
  if (!shortQuote.value) return '等待报价…'
  if (!shortQuote.value.executable || shortQuote.value.affordable === false) return '本笔暂不能成交'
  if (shortAction.value === 'cover' && coverAll.value) return '回补全部欠币'
  return `${shortAction.value === 'open' ? '借币做空' : '回补'} ${fxOrderAmountLabel(shortQuote.value.actual_foreign_amount)} ${currencyName.value}`
})
const pendingShortInterest = computed(() => {
  const row = shortPosition.value
  if (!row || row.pending_short_debt == null) return null
  const accrued = subtractFxAmounts(row.pending_short_debt, row.principal_foreign)
  return accrued == null ? null : subtractFxAmounts(accrued, row.interest_foreign)
})

const isClosing = computed(() =>
  tradeMode.value === 'spot' ? side.value === 'sell' : shortAction.value === 'cover',
)
const hasShortHolding = computed(
  () =>
    shortPosition.value?.pair_id === pairId.value &&
    compareFxAmounts(shortPosition.value.pending_short_debt, '0') === 1,
)
const fundingPreview = computed(() =>
  tradeMode.value === 'spot' &&
  side.value === 'buy' &&
  !summaryLoading.value &&
  !summaryFailed.value
    ? fxFundingPreview(amount.value, summary.value)
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
const orderMove = computed(() => {
  if (isClosing.value || snapshotLoading.value || snapshotFailed.value || !snapshot.value)
    return null
  const quantity =
    tradeMode.value === 'spot'
      ? !quoting.value && !quoteExpired.value && quote.value?.side === 'buy'
        ? quote.value.output_amount
        : null
      : !shortQuoting.value && !shortExpired.value && shortQuote.value?.action === 'open'
        ? shortQuote.value.requested_foreign_amount
        : null
  const exposure = quantity == null ? null : multiplyFxAmount(quantity, snapshot.value.price)
  return exposure == null ? null : multiplyFxAmount(exposure, '0.01')
})
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
const quotedShortRiskLabel = computed(() =>
  shortQuote.value
    ? riskLabels[fxShortRiskStatus(shortQuote.value, shortPostMargin.value)]
    : '等待报价',
)

function selectDirection(mode: 'spot' | 'short') {
  if (submitting.value || pendingRisk.value) return
  if (mode === 'spot') setSide('buy')
  else {
    if (shortAction.value !== 'open') {
      shortAmount.value = ''
      shortGoldAmount.value = ''
    }
    shortAction.value = 'open'
    coverAll.value = false
    tradeMode.value = 'short'
  }
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
  if (next === 'sell') {
    if (hasSpotHolding.value) await prepareSpotSellAll()
    else await prepareShortCover(100)
  } else {
    selectDirection('spot')
    await focusTradePanel()
  }
}
async function loadShort() {
  const pid = pairId.value
  if (!pid) return
  const holdingGeneration = holdingAuthGeneration.value
  const generation = ++shortReadGeneration
  shortLoading.value = true
  try {
    const row = await fxApi.getShort(pid)
    if (pid === pairId.value && generation === shortReadGeneration) {
      shortPosition.value = row
      shortFailed.value = false
      shortHoldingGeneration.value = holdingGeneration
    }
  } catch {
    if (pid === pairId.value && generation === shortReadGeneration) {
      shortFailed.value = true
      // Snapshot failures belong to the holdings warning. A successful cover
      // quote may still be executable, so do not label its order as failed.
    }
  } finally {
    if (generation === shortReadGeneration) shortLoading.value = false
  }
}
watch(
  [shortAction, shortAmount, coverAll, pairId],
  () => {
    shortError.value = ''
  },
  { flush: 'sync' },
)
function syncBorrowState() {
  borrowPending.value = borrowSubmitter.pending
  borrowUnreadable.value = borrowSubmitter.unreadable
  borrowUnresolved.value = borrowSubmitter.hasUnresolved
}
async function sendBorrowBuy(run: () => Promise<FxBorrowBuyTrade | null>) {
  if (submitting.value) return
  const generation = borrowGeneration
  submitting.value = true
  borrowError.value = ''
  let completed = false
  try {
    const promise = run()
    syncBorrowState()
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
    syncBorrowState()
    await refreshAll()
    if (generation !== borrowGeneration) return
    chartReloadToken.value++
    if (summaryFailed.value || walletFailed.value || tradesFailed.value || snapshotFailed.value)
      borrowError.value = '借款买入已完成，部分账户或行情信息刷新失败，请更新账户后再下单。'
    else msg.success(result.replay ? '已确认原融资成交' : '借款并买入已成交')
  } catch (e) {
    if (generation !== borrowGeneration) return
    borrowError.value = completed
      ? '借款买入已完成，账户信息刷新失败。'
      : borrowSubmitter.hasUnresolved
        ? '成交结果尚未确认，请重试原订单核对同一笔成交；期间不能提交新的交易。'
        : mapFxError(e, '借款买入失败，请重新报价')
  } finally {
    if (generation === borrowGeneration) {
      syncBorrowState()
      submitting.value = false
    }
  }
}
function submitBorrowBuy(body: Omit<FxBorrowBuyRequest, 'idempotency_key'>) {
  if (
    !pairId.value ||
    submitting.value ||
    pendingRisk.value ||
    borrowUnreadable.value ||
    summaryFailed.value ||
    summaryLoading.value ||
    !summary.value
  )
    return
  const pid = pairId.value
  void sendBorrowBuy(() =>
    borrowSubmitter.start(pid, body, (r) => fxApi.borrowBuy(r.pairId, r.body)),
  )
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
  await sendShortRequest(() => shortSubmitter.start(pid, action, body, executePendingShort))
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
  submitting.value = true
  try {
    const result = await run()
    if (!result) return
    shortReceipt.value = result
    shortQuote.value = null
    shortAmount.value = ''
    shortGoldAmount.value = ''
    shortError.value = ''
    msg.success(result.purpose === 'short_open' ? '开空已成交，所得已锁定用于回补' : '回补已成交')
    await refreshAll()
    chartReloadToken.value++
  } catch (e) {
    shortQuote.value = null
    shortError.value = shortSubmitter.pending
      ? '成交结果尚未确认，请重试原请求以查询同一笔成交；期间不能提交新的空头订单。'
      : mapFxError(e, '空头成交失败，请重新报价')
  } finally {
    pendingShort.value = shortSubmitter.pending
    shortStorageBlocked.value = shortSubmitter.unreadable || !authStore.user?.id
    submitting.value = false
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

async function loadWallet() {
  const pid = pairId.value
  if (pid) {
    const holdingGeneration = holdingAuthGeneration.value
    await walletResource.load(() => fxApi.getWallet(pid))
    if (holdingGeneration === holdingAuthGeneration.value)
      walletHoldingGeneration.value = holdingGeneration
  }
}

async function loadSummary() {
  await summaryResource.load(() => userApi.getSummary())
}

async function refreshAll() {
  await Promise.allSettled([loadSnapshot(), loadTrades(), loadWallet(), loadSummary(), loadShort()])
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
        void refreshAll()
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
  shortReadGeneration++
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
  const referencePrice =
    !snapshotFailed.value && !snapshotLoading.value && snapshot.value?.pair.id === pid
      ? snapshot.value.price
      : null
  if (
    !pid ||
    tradeMode.value !== 'spot' ||
    borrowing.value ||
    !tradable.value ||
    !amountValid.value
  ) {
    quote.value = null
    return
  }
  const gen = ++quoteGen
  quote.value = null
  quoting.value = true
  try {
    const q = await fxApi.getQuote(pid, { side: requestedSide, amount: value })
    if (gen === quoteGen) {
      if (
        q.pair_id !== pid ||
        q.side !== requestedSide ||
        compareFxAmounts(q.input_amount, value) !== 0
      ) {
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
  if (!pid || submitting.value || pendingRisk.value) return
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
    if (
      !q ||
      q.pair_id !== pid ||
      q.side !== side.value ||
      compareFxAmounts(q.input_amount, amount.value.trim()) !== 0
    ) {
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
    }
    msg.success(
      trade.side === 'buy'
        ? `买入成功，实际到账 ${formatFxAmount(trade.output_amount)} ${currencyName.value}`
        : `卖出成功，实际成交所得 ${formatFxAmount(trade.output_amount)} 金圆券`,
    )
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
  if (submitting.value) return
  tradeMode.value = 'spot'
  if (side.value === next) return
  amount.value = ''
  side.value = next
}

async function openShortTrade() {
  if (submitting.value) return
  selectDirection('short')
  await focusTradePanel()
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

watch([amount, side, pairId, tradeMode, borrowing], scheduleQuote, { flush: 'sync' })

onMounted(() => {
  window.addEventListener('resize', resizeWorkbench)
  void load().then(() => {
    // A cover deep link already initialized cover_all; only locate its form.
    if (route.query.action === 'cover') void focusTradePanel()
    else if (route.query.action === 'open') void openShortTrade()
  })
  freshnessTimer = setInterval(() => {
    now.value = Date.now()
  }, 1000)
})

onUnmounted(() => {
  window.removeEventListener('resize', resizeWorkbench)
  workbenchObserver?.disconnect()
  if (quoteTimer) clearTimeout(quoteTimer)
  quoteGen++
  snapshotGeneration++
  selectionGeneration++
  shortReadGeneration++
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
          本金（净值）
          <strong>{{
            summaryLoading
              ? '刷新中…'
              : summaryFailed
                ? '更新失败'
                : formatFxAmount(summary?.liquidation_equity, 2)
          }}</strong>
        </div>
        <div>
          可用现金
          <strong>{{
            summaryLoading
              ? '刷新中…'
              : summaryFailed
                ? '更新失败'
                : formatFxAmount(fxAvailableCash(summary), 2)
          }}</strong>
        </div>
        <div>
          账户风险
          <strong :class="{ 'fx-error': ['warning', 'danger', 'blocked'].includes(accountRisk) }">{{
            accountRiskLabel
          }}</strong>
        </div>
      </div>
      <div class="fx-market-volume">
        <span>24 小时成交额</span
        ><strong>{{ formatFxAmount(snapshot?.volume_24h, 2) }} <small>金圆券</small></strong>
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

    <div v-if="borrowPending" class="fx-pending-short" role="status">
      <div>
        <strong>一笔借款买入订单尚未确认</strong>
        <p>
          货币对 {{ borrowPending.pairId }}，投入
          {{ formatFxAmount(borrowPending.body.amount) }} 金圆券， 新增借款
          {{ formatFxAmount(borrowPending.body.borrow_amount) }}。重试沿用原订单编号。
        </p>
      </div>
      <button class="btn-secondary" :disabled="submitting" @click="retryBorrowBuy">
        {{ submitting ? '核对中…' : '重试原订单' }}
      </button>
    </div>
    <p v-if="borrowUnreadable" class="fx-error" role="alert">
      融资订单记录无法安全读取或保存，暂不能新增融资。
      {{
        borrowUnresolved && !borrowPending ? '请核对成交记录并联系管理员，当前暂停新增交易。' : ''
      }}
    </p>
    <p v-if="borrowError" class="fx-error" role="status">{{ borrowError }}</p>
    <section v-if="borrowReceipt" class="fx-receipt" role="status">
      <h2>{{ borrowReceipt.trade.replay ? '原借款买入已确认' : '借款并买入已成交' }}</h2>
      <p>
        使用现金 {{ formatFxAmount(borrowReceipt.trade.cash_amount) }}，新增借款
        {{ formatFxAmount(borrowReceipt.trade.borrow_amount) }} 金圆券， 实际买入
        {{ formatFxAmount(borrowReceipt.trade.output_amount) }} {{ borrowReceipt.currency }}。
        手续费 {{ formatFxAmount(borrowReceipt.trade.fee_amount) }} 金圆券（已含）。
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
                height="100%"
              />
            </div>
            <div class="fx-chart-footer">
              <span>上涨有利于多头，下跌有利于空头</span><span>金圆券 / {{ currencyName }}</span>
            </div>
          </section>
        </div>

        <aside class="fx-trade-panel" aria-labelledby="fx-trade-heading">
          <h2 id="fx-trade-heading" ref="tradePanelRef" class="fx-trade-anchor" tabindex="-1">
            {{ isClosing ? '平仓' : '开仓' }} {{ currencyName }}
          </h2>
          <div class="fx-direction-picker fx-action-picker" role="group" aria-label="选择交易操作">
            <button
              class="fx-direction-long"
              :class="{ active: tradeMode === 'spot' && !isClosing }"
              :aria-pressed="tradeMode === 'spot' && !isClosing"
              :disabled="submitting || pendingRisk"
              @click="selectDirection('spot')"
            >
              <span aria-hidden="true">↗</span><strong>买入做多</strong>
            </button>
            <button
              class="fx-direction-short"
              :class="{ active: tradeMode === 'short' && !isClosing }"
              :aria-pressed="tradeMode === 'short' && !isClosing"
              :disabled="submitting || pendingRisk"
              @click="selectDirection('short')"
            >
              <span aria-hidden="true">↘</span><strong>借币做空</strong>
            </button>
            <button
              class="fx-close-choice fx-close-choice--sell"
              :class="{ active: tradeMode === 'spot' && isClosing }"
              :aria-pressed="tradeMode === 'spot' && isClosing"
              :disabled="!canSellSpotHolding"
              @click="prepareSpotSellAll(100)"
            >
              <strong>卖出多头</strong>
            </button>
            <button
              class="fx-close-choice"
              :class="{ active: tradeMode === 'short' && isClosing }"
              :aria-pressed="tradeMode === 'short' && isClosing"
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
              <article
                v-if="hasSpotHolding"
                class="fx-holding-box"
                :class="`fx-holding-box--${spotPnlDirection}`"
              >
                <dl class="fx-holding-meta">
                  <div>
                    <dt>多头持仓</dt>
                    <dd>{{ fxOrderAmountLabel(wallet?.foreign_amount) }} {{ currencyName }}</dd>
                  </div>
                  <div>
                    <dt>买入均价</dt>
                    <dd>{{ formatFxPrice(walletAvgCost) }}</dd>
                  </div>
                  <div>
                    <dt>账面市值</dt>
                    <dd>{{ formatFxAmount(holdingValue?.marketValue, 2) }} 金圆券</dd>
                  </div>
                </dl>
                <div class="fx-holding-pnl-row">
                  <div class="fx-holding-pnl">
                    <span>账面盈亏</span
                    ><strong :class="`fx-spot-pnl--${spotPnlDirection}`"
                      >{{ spotPnlDirection === 'up' ? '+' : ''
                      }}{{ formatFxAmount(holdingValue?.pnl, 2) }} <small>金圆券</small></strong
                    >
                  </div>
                  <button
                    type="button"
                    class="fx-quick-close"
                    :disabled="!canSellSpotHolding"
                    @click="prepareSpotSellAll(100)"
                  >
                    一键平仓<small>卖出全部 {{ currencyName }}</small>
                  </button>
                </div>
                <p>按参考汇率估值，卖出费用和价格影响以平仓报价为准。</p>
              </article>
              <article
                v-if="hasShortHolding"
                class="fx-holding-box"
                :class="{
                  'fx-holding-box--up': compareFxAmounts(shortHoldingPnl, '0') === 1,
                  'fx-holding-box--down': compareFxAmounts(shortHoldingPnl, '0') === -1,
                }"
              >
                <dl class="fx-holding-meta">
                  <div>
                    <dt>空头待还（含息）</dt>
                    <dd>
                      {{ fxOrderAmountLabel(shortPosition?.pending_short_debt) }} {{ currencyName }}
                    </dd>
                  </div>
                  <div>
                    <dt>锁定所得</dt>
                    <dd>{{ formatFxAmount(shortPosition?.restricted_gold, 2) }} 金圆券</dd>
                  </div>
                  <div>
                    <dt>参考回补成本</dt>
                    <dd>{{ formatFxAmount(shortPosition?.reference_cover_cost, 2) }} 金圆券</dd>
                  </div>
                </dl>
                <div class="fx-holding-pnl-row">
                  <div class="fx-holding-pnl">
                    <span>预计平仓盈亏</span
                    ><strong
                      :class="{
                        up: compareFxAmounts(shortHoldingPnl, '0') === 1,
                        down: compareFxAmounts(shortHoldingPnl, '0') === -1,
                      }"
                      >{{ compareFxAmounts(shortHoldingPnl, '0') === 1 ? '+' : ''
                      }}{{ formatFxAmount(shortHoldingPnl, 2) }} <small>金圆券</small></strong
                    >
                  </div>
                  <button
                    type="button"
                    class="fx-quick-close"
                    :disabled="!canFillCoverAll"
                    @click="prepareShortCover(100)"
                  >
                    一键平仓<small>回补全部欠币</small>
                  </button>
                </div>
                <p>包含参考回补费用和欠币利息，成交以本笔报价为准。</p>
              </article>
              <p
                v-if="!shortFailed && !shortLoading && shortPosition?.pending_short_debt === null"
                class="fx-hint"
              >
                空头欠币待估值，可选择全部回补获取报价。
              </p>
              <p
                v-else-if="
                  !walletLoading &&
                  !shortLoading &&
                  !walletFailed &&
                  !shortFailed &&
                  wallet &&
                  shortPosition &&
                  !hasSpotHolding &&
                  !hasShortHolding
                "
                class="fx-no-position"
              >
                暂无 {{ currencyName }} 持仓，买入或做空后在此查看盈亏。
              </p>
              <p v-else-if="!wallet || !shortPosition" class="fx-hint">等待持仓数据。</p>
            </section>
            <details
              v-if="
                hasSpotHolding ||
                hasShortHolding ||
                walletFailed ||
                shortFailed ||
                shortPosition?.pending_short_debt === null
              "
              class="fx-position-details"
            >
              <summary>持仓、利息与估值明细</summary>
              <section class="fx-holdings" aria-labelledby="fx-holdings-title">
                <div class="fx-panel-head">
                  <h2 id="fx-holdings-title">我的 {{ currencyName }} 持仓</h2>
                </div>
                <p v-if="walletLoading || shortLoading" class="fx-hint" role="status">
                  正在刷新持仓，已有数字为上次快照…
                </p>
                <p v-if="walletFailed" class="fx-error" role="alert">多头持仓更新失败。</p>
                <p v-if="shortFailed" class="fx-error" role="alert">空头持仓更新失败。</p>
                <article v-if="hasSpotHolding" class="fx-position-card" aria-label="多头持仓">
                  <div class="fx-position-heading">
                    <div>
                      <span class="fx-position-tag fx-position-tag--long">↗ 多头</span
                      ><strong>{{ currencyName }}</strong>
                    </div>
                    <div class="fx-spot-pnl">
                      <span>账面盈亏</span
                      ><strong :class="`fx-spot-pnl--${spotPnlDirection}`"
                        >{{ spotPnlDirection === 'up' ? '+' : ''
                        }}{{ formatFxAmount(holdingValue?.pnl, 2) }} <small>金圆券</small></strong
                      >
                    </div>
                  </div>
                  <dl class="fx-position-meta">
                    <div>
                      <dt>持仓数量</dt>
                      <dd>{{ formatFxAmount(wallet?.foreign_amount) }} {{ currencyName }}</dd>
                    </div>
                    <div>
                      <dt>买入均价</dt>
                      <dd>{{ formatFxPrice(walletAvgCost) }}</dd>
                    </div>
                    <div>
                      <dt>账面市值</dt>
                      <dd>{{ formatFxAmount(holdingValue?.marketValue, 2) }} 金圆券</dd>
                    </div>
                  </dl>
                  <details class="fx-details">
                    <summary>持仓估值与费用说明</summary>
                    <p>账面盈亏不含卖出费用与价格影响，实际所得以平仓报价为准。</p>
                    <p>
                      {{ walletUpdatedAt ? `持仓读取于 ${walletUpdatedAt}。` : ''
                      }}{{
                        snapshotFailed
                          ? '行情更新失败，按上次行情估值。'
                          : snapshotLoading
                            ? '行情刷新中，按上次行情估值。'
                            : '按当前边际汇率估值。'
                      }}
                    </p>
                  </details>
                </article>
                <article v-if="hasShortHolding" class="fx-position-card" aria-label="空头持仓">
                  <div class="fx-position-heading">
                    <div>
                      <span class="fx-position-tag fx-position-tag--short">↘ 空头</span
                      ><strong>{{ currencyName }}</strong>
                    </div>
                    <ShortPositionPnl
                      :proceeds-basis-gold="shortPosition!.proceeds_basis_gold"
                      :reference-cover-cost="shortPosition!.reference_cover_cost"
                    />
                  </div>
                  <dl class="fx-position-meta">
                    <div>
                      <dt>待归还（含息）</dt>
                      <dd>
                        {{ formatFxAmount(shortPosition?.pending_short_debt) }} {{ currencyName }}
                      </dd>
                    </div>
                    <div>
                      <dt>锁定卖出所得</dt>
                      <dd>{{ formatFxAmount(shortPosition?.restricted_gold, 2) }} 金圆券</dd>
                    </div>
                    <div>
                      <dt>参考买回成本</dt>
                      <dd>{{ formatFxAmount(shortPosition?.reference_cover_cost, 2) }} 金圆券</dd>
                    </div>
                  </dl>
                  <details class="fx-details">
                    <summary>本金、利息与盈亏说明</summary>
                    <div class="fx-preview-row">
                      <span>外币本金</span
                      ><strong
                        >{{ formatFxAmount(shortPosition?.principal_foreign) }}
                        {{ currencyName }}</strong
                      >
                    </div>
                    <div class="fx-preview-row">
                      <span>已结 / 待计利息</span
                      ><strong
                        >{{ formatFxAmount(shortPosition?.interest_foreign) }} /
                        {{ formatFxAmount(pendingShortInterest) }} {{ currencyName }}</strong
                      >
                    </div>
                    <p>
                      盈亏以剩余开仓所得减去参考买回成本估算，包含回补估值中的利息、手续费及价格影响，实际以成交为准。
                    </p>
                    <p v-if="!shortPosition?.executable">
                      全仓参考报价不可执行，获取本笔平仓报价后查看具体原因。
                    </p>
                  </details>
                </article>
                <p
                  v-if="
                    !shortFailed &&
                    !shortLoading &&
                    shortPosition &&
                    shortPosition.pending_short_debt === null
                  "
                  class="fx-error"
                  role="status"
                >
                  空头欠币暂无法估值，可尝试获取全部平仓报价。
                </p>
                <div
                  v-if="
                    !walletLoading &&
                    !shortLoading &&
                    !walletFailed &&
                    !shortFailed &&
                    wallet &&
                    shortPosition &&
                    compareFxAmounts(shortPosition.pending_short_debt, '0') === 0 &&
                    !hasSpotHolding
                  "
                  class="fx-empty-holdings"
                >
                  <span aria-hidden="true">↗ ↘</span>
                  <div>
                    <strong>还没有 {{ currencyName }} 持仓</strong>
                    <p>成交后在这里查看持仓与盈亏。</p>
                  </div>
                </div>
              </section>
            </details>
            <div class="fx-order-heading">
              <h2 class="fx-trade-heading">
                {{ isClosing ? '平仓' : '开仓' }}
              </h2>
              <label v-if="tradeMode === 'short' && shortAction === 'open'" class="fx-short-unit"
                >规模单位
                <select
                  :value="shortSizeUnit"
                  aria-label="做空金额单位"
                  :disabled="submitting || pendingRisk"
                  @change="
                    setShortSizeUnit(
                      ($event.target as HTMLSelectElement).value as 'gold' | 'foreign',
                    )
                  "
                >
                  <option value="gold">金圆券</option>
                  <option value="foreign">{{ currencyName }}</option>
                </select></label
              >
              <span v-else>交易 {{ currencyName }}</span>
            </div>
            <FxOrderSize
              v-if="tradeMode === 'spot' && side === 'buy'"
              v-model="amount"
              :principal="orderPrincipal"
              :limit="orderMultiplierLimit"
              tone="long"
              label="买入总额（含手续费）"
              :disabled="!tradable || submitting || pendingRisk"
            />
            <FxBorrowBuyForm
              v-show="tradeMode === 'spot' && side === 'buy' && borrowing"
              :key="authStore.user?.id ?? 'anonymous'"
              :pair-id="pairId"
              :currency="currencyName"
              :amount="amount"
              :borrow-amount="fundingPreview?.shortfall ?? ''"
              :reference-price="
                !snapshotFailed && !snapshotLoading && snapshot?.pair.id === pairId
                  ? snapshot.price
                  : null
              "
              :submitting="submitting"
              :pending="pendingRisk"
              :active="tradeMode === 'spot' && side === 'buy' && borrowing"
              :blocked="
                submitting || pendingRisk || borrowUnreadable || !tradable || hasShortHolding
              "
              :blocked-reason="
                hasShortHolding
                  ? '请先回补当前币种的空头，再借款买入。'
                  : !tradable
                    ? '当前币种暂不允许买入。'
                    : ''
              "
              :account-ready="
                !summaryLoading &&
                !summaryFailed &&
                !!summary &&
                !snapshotFailed &&
                !snapshotLoading
              "
              @submit="submitBorrowBuy"
            />
            <div
              v-show="tradeMode === 'spot' && !(side === 'buy' && borrowing)"
              class="fx-trade-body"
            >
              <template v-if="side === 'sell'">
                <label class="fx-field"
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
                <div class="fx-sell-shortcuts">
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
                <p class="fx-field-note">按当前多头持仓比例填写；确认前可修改数量。</p>
              </template>
              <div class="fx-quote-box" aria-label="交易报价">
                <div v-if="side === 'buy'" class="fx-funding-preview">
                  <div>
                    <span>现金投入</span
                    ><strong>{{ formatFxAmount(fundingPreview?.cashInput, 2) }}</strong>
                  </div>
                  <div>
                    <span>新增借款</span
                    ><strong>{{ formatFxAmount(fundingPreview?.shortfall, 2) }}</strong>
                  </div>
                </div>
                <p v-if="summaryFailed" class="fx-error" role="alert">
                  账户更新失败，暂不能做多。<button
                    class="fx-text-button"
                    :disabled="summaryLoading"
                    @click="loadSummary"
                  >
                    重新加载
                  </button>
                </p>
                <FxExecutionPrice
                  :average="effectiveGoldPerForeign"
                  :reference="quoteReferencePrice"
                  :currency="currencyName"
                />
                <div class="fx-preview fx-order-summary">
                  <div class="fx-preview-row">
                    <span>{{ side === 'buy' ? '预计买入' : '预计卖出所得' }}</span
                    ><strong
                      >{{ quote ? formatFxAmount(quote.output_amount) : '—' }}
                      {{ outputCurrency }}</strong
                    >
                  </div>
                  <div class="fx-quote-meta">
                    <div class="fx-preview-row">
                      <span>手续费（已含）</span
                      ><strong
                        >{{ quote ? formatFxAmount(quote.fee_amount) : '—' }}
                        {{ inputCurrency }}</strong
                      >
                    </div>
                    <div v-if="side === 'buy'" class="fx-preview-row">
                      <span>当前账户风险</span
                      ><strong
                        :class="{
                          'fx-error': ['warning', 'danger', 'blocked'].includes(accountRisk),
                        }"
                        >{{ accountRiskLabel }}</strong
                      >
                    </div>
                  </div>
                </div>
                <FxClosePnl
                  v-if="side === 'sell'"
                  :pnl="spotClosePreview?.pnl ?? null"
                  :note="
                    spotClosePreview?.full
                      ? '含报价手续费，未扣金圆券借款利息。'
                      : '按卖出数量分摊成本；含报价手续费，未扣金圆券借款利息。'
                  "
                  :unavailable="spotCloseUnavailable"
                />
              </div>
              <div v-if="side === 'sell' && summary" class="fx-preview">
                <div class="fx-preview-row">
                  <span>预计用于还款</span
                  ><strong>{{ formatFxAmount(sellAllocation?.repayment) }} 金圆券</strong>
                </div>
                <div class="fx-preview-row">
                  <span>预计现金增加</span
                  ><strong>{{ formatFxAmount(sellAllocation?.cashIncrease) }} 金圆券</strong>
                </div>
                <p class="fx-hint">按最近账户快照估算，实际还款以账户记录为准。</p>
              </div>
              <p v-if="tradeBlockReason" class="fx-error" role="status">{{ tradeBlockReason }}</p>
              <div class="fx-submit-state" role="status">
                <span v-if="tradeError" class="fx-error">{{ tradeError }}</span
                ><span v-else-if="submitting" class="fx-hint">订单处理中…</span
                ><span v-else-if="quoting" class="fx-hint">正在更新报价…</span
                ><span v-else-if="amount && !amountValid" class="fx-hint"
                  >请输入正数，最多 6 位小数。</span
                ><span v-else-if="quoteExpired" class="fx-error">报价已过期，请重新报价。</span
                ><span v-else-if="!amount" class="fx-hint">填写金额后自动报价。</span>
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
                  {{ spotSubmitLabel }}</button
                ><button
                  class="fx-text-button"
                  :disabled="quoting || submitting || !tradable || !amountValid || pendingRisk"
                  @click="fetchQuote"
                >
                  重新报价
                </button>
              </div>
              <details class="fx-details fx-order-details">
                <summary>费用、报价与风控明细</summary>
                <div v-if="orderMove && side === 'buy'" class="fx-payoff-preview">
                  <span>本笔汇率变化 1%</span>
                  <div>
                    <span
                      >上涨 1% <strong class="up">+{{ formatFxAmount(orderMove, 2) }}</strong></span
                    ><span
                      >下跌 1%
                      <strong class="down">−{{ formatFxAmount(orderMove, 2) }}</strong></span
                    >
                  </div>
                  <small>价格盈亏示意，未扣费用与利息</small>
                </div>

                <p>
                  本金以账户清算净值为基准，本次倍数只用于选规模，可否成交以报价为准。全账户共同担保，亏损可能影响其他持仓。
                </p>
                <div class="fx-preview-row">
                  <span>参考{{ side === 'buy' ? '买入' : '卖出' }}价</span
                  ><strong>{{ formatFxPrice(sidePrice) }} 金圆券 / {{ currencyName }}</strong>
                </div>
                <div class="fx-preview-row">
                  <span>{{ side === 'buy' ? '最低买入数量' : '最低卖出所得' }}</span
                  ><strong>{{ minOutDisplay }} {{ outputCurrency }}</strong>
                </div>
                <label class="fx-field fx-field--inline"
                  ><span>报价变动容忍度</span
                  ><select
                    v-model.number="slippageBps"
                    class="fx-input"
                    :disabled="!tradable || submitting"
                  >
                    <option :value="50">0.5%</option>
                    <option :value="100">1%</option>
                    <option :value="200">2%</option>
                    <option :value="500">5%</option>
                  </select></label
                >
                <p>
                  {{
                    quoteUpdatedAt
                      ? `报价更新于 ${new Date(quoteUpdatedAt).toLocaleTimeString()}，30 秒内可提交。`
                      : '填写金额后自动报价。'
                  }}实际所得低于最低金额时交易会取消。
                </p>
                <p>手续费从投入中扣除。当前账户风险来自最近快照，做多能否成交以提交时检查为准。</p>
                <CreditRiskStatus
                  v-if="summary"
                  :ratio="
                    summaryLoading || summaryFailed ? null : (summary.equity_to_risk_basis ?? null)
                  "
                  :initial="summary.r_initial ?? null"
                  :maintenance="summary.r_maintenance ?? null"
                  :authoritative-status="
                    summaryLoading || summaryFailed ? 'unknown' : summary.risk_status
                  "
                  :no-risk="
                    !summaryLoading &&
                    !summaryFailed &&
                    compareFxAmounts(summary.risk_basis, '0') === 0
                  "
                />
              </details>
            </div>

            <div v-show="tradeMode === 'short'" class="fx-trade-body">
              <p v-if="shortStorageBlocked" class="fx-error" role="alert">
                {{
                  authStore.user?.id
                    ? '待确认订单记录无法安全读取或保存，请核对成交历史并联系管理员。'
                    : '请先登录后使用空头交易。'
                }}
              </p>
              <div v-if="shortAction === 'open'" class="fx-short-size">
                <FxOrderSize
                  v-if="shortSizeUnit === 'gold'"
                  :model-value="shortGoldAmount"
                  :principal="orderPrincipal"
                  :limit="orderMultiplierLimit"
                  tone="short"
                  label="做空参考规模"
                  :disabled="
                    !canFillShortOpen ||
                    hasSpotHolding ||
                    activePair?.status !== 'trading' ||
                    activePair?.reduce_only === true
                  "
                  @update:model-value="updateShortGoldAmount"
                />
                <input
                  v-else
                  id="fx-short-foreign"
                  aria-label="借入并卖出数量"
                  v-model="shortAmount"
                  class="fx-input fx-size-input"
                  inputmode="decimal"
                  autocomplete="off"
                  placeholder="0"
                  :disabled="submitting || pendingRisk || shortStorageBlocked"
                />
                <div class="fx-preview-row fx-short-quantity">
                  <span>借入并卖出</span
                  ><strong
                    >{{ shortAmount ? formatFxAmount(shortAmount) : '—' }}
                    {{ currencyName }}</strong
                  >
                </div>
                <p v-if="hasSpotHolding" class="fx-error">请先平掉当前多头，再做空这个币种。</p>
              </div>
              <div v-else>
                <label class="fx-field"
                  ><span
                    >买回归还数量 <small>{{ currencyName }}</small></span
                  ><input
                    v-model="shortAmount"
                    class="fx-input fx-size-input"
                    inputmode="decimal"
                    autocomplete="off"
                    :disabled="submitting || pendingRisk || coverAll"
                    :placeholder="coverAll ? '全部欠币（含成交时利息）' : '0'"
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
                    {{ percent === 100 ? '全部平仓' : `${percent}%` }}
                  </button>
                </div>
                <label class="fx-cover-all"
                  ><input
                    v-model="coverAll"
                    type="checkbox"
                    :disabled="submitting || pendingRisk"
                  />全部归还，包含成交时新计利息</label
                >
                <p v-if="shortFailed" class="fx-error">
                  持仓更新失败，可选择全部平仓获取最新报价。
                </p>
              </div>
              <div class="fx-quote-box" aria-label="空头交易报价">
                <div class="fx-short-funding">
                  <div class="fx-preview-row">
                    <span>{{ shortAction === 'open' ? '卖出所得锁定' : '预计买回成本' }}</span
                    ><strong
                      >{{
                        shortQuote
                          ? formatFxAmount(
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
                        ? '锁定资金用于买回归还，不能花用。'
                        : '先使用本仓锁金，不足部分使用可用现金。'
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
                      >{{ shortQuote ? formatFxAmount(shortQuote.fee_amount) : '—' }}
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
                  <div class="fx-preview-row">
                    <span>成交后账户风险</span
                    ><strong
                      :class="{ 'fx-error': shortQuote && shortQuote.margin_status !== 'healthy' }"
                      >{{ quotedShortRiskLabel }}</strong
                    >
                  </div>
                </div>
                <FxPostTradeMargin
                  :equity="shortQuote?.estimated_equity"
                  :basis="shortQuote?.estimated_risk_basis"
                  :initial="!summaryFailed && !summaryLoading ? summary?.r_initial : null"
                  :caution="!!shortQuote && shortQuote.margin_status !== 'healthy'"
                />
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
              <p v-if="summaryFailed" class="fx-error">
                账户更新失败，报价会重新检查资金与风险。<button
                  class="fx-text-button"
                  :disabled="summaryLoading"
                  @click="loadSummary"
                >
                  重试
                </button>
              </p>
              <p v-if="shortQuote?.blocked_reason" class="fx-error">
                {{ shortBlockMessage(shortQuote.blocked_reason) }}
              </p>
              <p v-if="shortQuote?.affordable === false" class="fx-error">
                锁定资金与可用现金不足，请减少买回数量或补充现金。
              </p>
              <p v-if="shortQuote?.risk_blocked_reason" class="fx-hint">
                {{ shortBlockMessage(shortQuote.risk_blocked_reason) }}
              </p>
              <div class="fx-submit-state" role="status">
                <span v-if="shortError || shortQuoteError" class="fx-error">{{
                  shortError || shortQuoteError
                }}</span
                ><span v-else-if="shortQuoting" class="fx-hint">正在更新报价…</span
                ><span v-else-if="shortExpired" class="fx-error">报价已过期，请重新报价。</span
                ><span v-else-if="!shortQuote" class="fx-hint">{{
                  shortQuoteOrder ? '等待自动报价…' : '填写金额后自动报价。'
                }}</span>
              </div>
              <div class="fx-actions">
                <button class="fx-submit" :disabled="!shortCanSubmit" @click="submitShort">
                  {{ shortSubmitLabel }}</button
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
                <summary>费用、利息与风控明细</summary>
                <p v-if="shortAction === 'open'" class="fx-field-note">
                  {{
                    shortSizeUnit === 'gold'
                      ? `按输入时汇率换算，借入 ${shortAmount ? formatFxAmount(shortAmount) : '—'} ${currencyName}；实际卖出所得以报价为准。`
                      : '外币借入后立即卖出，实际所得以报价为准。'
                  }}
                </p>
                <div v-if="orderMove && shortAction === 'open'" class="fx-payoff-preview">
                  <span>本笔汇率变化 1%</span>
                  <div>
                    <span
                      >上涨 1%
                      <strong class="down">−{{ formatFxAmount(orderMove, 2) }}</strong></span
                    ><span
                      >下跌 1% <strong class="up">+{{ formatFxAmount(orderMove, 2) }}</strong></span
                    >
                  </div>
                  <small>价格盈亏示意，未扣费用与利息</small>
                </div>

                <p>
                  全账户共同担保，亏损可能影响其他持仓。本金以账户清算净值为基准，倍数只用于选规模，可否成交以报价为准。
                </p>
                <div class="fx-preview-row">
                  <span>可用现金</span
                  ><strong
                    >{{
                      summaryLoading || summaryFailed
                        ? '数据待恢复'
                        : formatFxAmount(fxAvailableCash(summary))
                    }}
                    金圆券</strong
                  >
                </div>
                <div class="fx-preview-row">
                  <span>{{ shortAction === 'open' ? '最低卖出所得' : '最高买回支出' }}</span
                  ><strong>{{ shortLimit || '—' }} 金圆券</strong>
                </div>
                <label class="fx-field fx-field--inline"
                  ><span>报价变动容忍度</span
                  ><select v-model.number="slippageBps" class="fx-input" :disabled="submitting">
                    <option :value="50">0.5%</option>
                    <option :value="100">1%</option>
                    <option :value="200">2%</option>
                    <option :value="500">5%</option>
                  </select></label
                ><CreditRiskStatus
                  v-if="shortQuote"
                  title="成交后保证金率（预估）"
                  :ratio="shortPostMargin"
                  :initial="summary?.r_initial ?? null"
                  :maintenance="summary?.r_maintenance ?? null"
                  :authoritative-status="shortQuote.margin_status"
                  :no-risk="compareFxAmounts(shortQuote.estimated_risk_basis, '0') === 0"
                />
                <p v-if="shortQuote">
                  报价有效至 {{ formatTime(shortQuote.expires_at) }}，成交时重新检查价格与风险。
                </p>
                <p>欠币会产生利息，保证金不足时有强平风险。平仓不会自动借入金圆券。</p>
                <router-link to="/loan#short-debt">查看空头借款与账户规则</router-link>
              </details>
            </div>
          </div>
        </aside>
      </div>

      <section v-if="receipt" class="fx-receipt" role="status" aria-live="polite">
        <h2>
          {{ receipt.trade.side === 'buy' ? '做多已成交' : '多头平仓已成交' }}：{{
            receipt.currency
          }}
        </h2>
        <p>
          实际投入 {{ formatFxAmount(receipt.trade.input_amount) }}
          {{ receipt.trade.side === 'buy' ? '金圆券' : receipt.currency }}，{{
            receipt.trade.side === 'buy' ? '实际买入' : '实际卖出所得'
          }}
          {{ formatFxAmount(receipt.trade.output_amount) }}
          {{ receipt.trade.side === 'buy' ? receipt.currency : '金圆券' }}。
        </p>
        <details class="fx-details">
          <summary>成交明细</summary>
          <p>
            手续费 {{ formatFxAmount(receipt.trade.fee_amount) }}
            {{ receipt.trade.side === 'buy' ? '金圆券' : receipt.currency }}（已含），成交均价
            {{ formatFxPrice(fxGoldPerForeign(receipt.trade)) }} 金圆券 / {{ receipt.currency }}。
          </p>
          <p v-if="receipt.trade.side === 'sell'">
            所得优先还款，本笔实际还款与现金变化请核对账户记录。
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
          实际投入 {{ formatFxAmount(shortReceipt.input_amount) }}
          {{ shortReceipt.side === 'buy' ? '金圆券' : shortReceiptCurrency }}，产出
          {{ formatFxAmount(shortReceipt.output_amount) }}
          {{ shortReceipt.side === 'buy' ? shortReceiptCurrency : '金圆券' }}。手续费
          {{ formatFxAmount(shortReceipt.fee_amount) }}
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
                <tr v-for="t in trades ?? []" :key="t.id">
                  <td>{{ formatTime(t.created_at) }}</td>
                  <td>
                    {{
                      t.purpose === 'borrow_buy'
                        ? '借款做多'
                        : t.purpose === 'short_open'
                          ? '做空'
                          : t.purpose === 'short_cover'
                            ? '空头平仓'
                            : t.side === 'buy'
                              ? '做多'
                              : '多头平仓'
                    }}
                  </td>
                  <td>
                    {{ formatFxAmount(t.input_amount) }}
                    <small v-if="t.purpose === 'borrow_buy'"
                      >（借款 {{ formatFxAmount(t.borrow_amount) }} 金圆券）</small
                    >
                    {{ t.side === 'buy' ? '金圆券' : currencyName }}
                  </td>
                  <td>
                    {{ formatFxAmount(t.output_amount) }}
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
            <div v-for="t in trades ?? []" :key="t.id" class="fx-preview-row">
              <span>{{ formatTime(t.created_at) }} 手续费</span
              ><strong
                >{{ formatFxAmount(t.fee_amount) }}
                {{ t.side === 'buy' ? '金圆券' : currencyName }}；均价
                {{ formatFxPrice(fxGoldPerForeign(t)) }}</strong
              >
            </div>
          </details>
        </section>
      </div>
      <details class="fx-account-details fx-details">
        <summary>账户快照与借款规则</summary>
        <div class="fx-preview-row">
          <span>全部外币账面市值</span><strong>{{ formatFxAmount(summary?.fx_mtm) }} 金圆券</strong>
        </div>
        <div class="fx-preview-row">
          <span>全部外币浮动盈亏</span
          ><strong
            :class="(compareFxAmounts(summary?.fx_unrealized_pnl, '0') ?? 0) >= 0 ? 'up' : 'down'"
            >{{ formatFxAmount(summary?.fx_unrealized_pnl) }} 金圆券</strong
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
    <MobileTradeDock
      :visible="!loading && !error && !!activePair && !tradePanelVisible"
      :label="currencyName"
      :side="side"
      :disabled="submitting || pendingRisk"
      :show-short="true"
      :short-active="tradeMode === 'short'"
      :show-sell="hasSpotHolding || hasShortHolding"
      buy-label="做多"
      sell-label="平仓"
      short-label="做空"
      @select="openFxTrade"
      @short="openShortTrade"
    />
  </div>
</template>

<style scoped>
.fx-page {
  max-width: 1360px;
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
  font-size: 17px;
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
  flex: 1;
  min-height: 0;
  min-width: 0;
}
.fx-chart-footer {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  color: #888;
  font-size: 11px;
  padding-top: 10px;
}
.fx-holdings {
  padding: 12px 0 0;
  margin-top: 8px;
  border-top: 1px solid #ddd;
  background: #fff;
  color: #000;
  padding: 12px;
}
.fx-holdings > .fx-hint,
.fx-holdings > .fx-error {
  margin-bottom: 10px;
}
.fx-position-card {
  padding: 14px 0 18px;
  border-bottom: 1px solid #ddd;
}
.fx-position-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.fx-position-heading > div:first-child {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
}
.fx-position-tag {
  font-size: 11px;
  font-weight: 650;
  padding: 3px 7px;
  white-space: nowrap;
}
.fx-position-tag--long {
  background: var(--color-up-bg);
  color: var(--color-up);
}
.fx-position-tag--short {
  background: var(--color-down-bg);
  color: var(--color-down);
}
.fx-spot-pnl {
  text-align: right;
}
.fx-spot-pnl > span {
  display: block;
  font-size: 11px;
  color: #777;
}
.fx-spot-pnl > strong {
  font-size: 18px;
  line-height: 1.4;
}
.fx-spot-pnl small {
  font-size: 11px;
}
.fx-spot-pnl--up {
  color: var(--color-up);
}
.fx-spot-pnl--down {
  color: var(--color-down);
}
.fx-spot-pnl--flat {
  color: #555;
}
.fx-position-heading :deep(.short-pnl) {
  border: 0;
  padding: 0;
  text-align: right;
}
.fx-position-heading :deep(.short-pnl-label) {
  font-size: 11px;
  font-weight: 400;
  color: #777;
}
.fx-position-heading :deep(.short-pnl-amount) {
  font-size: 18px;
  line-height: 1.4;
  margin: 0;
  font-weight: 650;
}
.fx-position-heading :deep(.short-pnl-note) {
  display: none;
}
.fx-position-meta {
  display: grid;
  grid-template-columns: 1fr;
  gap: 10px;
  padding: 16px 0;
}
.fx-position-meta > div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: baseline;
}
.fx-position-meta dd {
  margin: 0;
  text-align: right;
}
.fx-position-meta dt {
  color: #777;
  font-size: 11px;
}
.fx-position-meta dd {
  margin-top: 4px;
  font-weight: 650;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.fx-position-bottom {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}
.fx-position-bottom > span {
  color: #888;
  font-size: 11px;
  max-width: 100%;
}
.fx-position-bottom > div {
  display: flex;
  gap: 7px;
  width: 100%;
}
.fx-position-bottom button {
  white-space: nowrap;
  flex: 1;
}
.fx-empty-holdings {
  padding: 26px 12px;
  display: flex;
  gap: 18px;
  align-items: center;
  border-bottom: 1px solid #ddd;
}
.fx-empty-holdings > span {
  color: #aaa;
  font-size: 26px;
  letter-spacing: 4px;
}
.fx-empty-holdings p {
  margin-top: 5px;
  color: #888;
  font-size: 12px;
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
.fx-current-position {
  display: grid;
  gap: 6px;
  margin-bottom: 8px;
}
.fx-holding-box {
  border: 1px solid #000;
  padding: 8px 10px;
  background: #fafafa;
}
.fx-holding-box--up {
  background: var(--color-up-bg);
}
.fx-holding-box--down {
  background: var(--color-down-bg);
}
.fx-holding-meta {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin: 0;
}
.fx-holding-meta dt,
.fx-holding-pnl > span {
  color: #666;
  font-size: 10px;
}
.fx-holding-meta dd {
  margin: 2px 0 0;
  font-size: 12px;
  font-weight: 700;
  overflow-wrap: anywhere;
}
.fx-holding-pnl-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  border-top: 1px solid #ddd;
  padding-top: 6px;
  margin-top: 6px;
}
.fx-holding-pnl {
  display: grid;
  gap: 2px;
  min-width: 0;
}
.fx-holding-pnl > strong {
  font-size: 18px;
  font-weight: 800;
  overflow-wrap: anywhere;
}
.fx-holding-pnl small {
  font-size: 10px;
  font-weight: 400;
}
.fx-quick-close {
  flex-shrink: 0;
  display: grid;
  gap: 2px;
  border: 2px solid #000;
  padding: 6px 8px;
  background: #fff;
  font-size: 12px;
  font-weight: 700;
  line-height: 1.2;
}
.fx-quick-close small,
.fx-holding-box > p {
  color: #777;
  font-size: 10px;
  font-weight: 400;
}
.fx-holding-box > p {
  margin: 4px 0 0;
  line-height: 1.4;
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
  font-size: 18px !important;
  font-weight: 700 !important;
  min-height: var(--trade-action-height);
}
.fx-size-input::placeholder {
  color: #aaa;
  font-size: 17px;
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
  font-size: 12px;
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
  padding-bottom: 6px;
}
.fx-quote-meta {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 6px 12px;
}
.fx-quote-meta .fx-preview-row {
  gap: 5px;
  font-size: 11px;
}
.fx-payoff-preview {
  padding: 10px 0 15px;
  border-bottom: 1px solid #eee;
  margin-bottom: 12px;
}
.fx-payoff-preview > span {
  font-size: 11px;
  color: #777;
}
.fx-payoff-preview > div {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin: 8px 0 4px;
  color: #666;
  font-size: 11px;
}
.fx-payoff-preview strong {
  font-size: 14px;
  margin-left: 5px;
}
.fx-payoff-preview > small {
  color: #999;
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
  padding-bottom: 6px;
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
  font-weight: 700 !important;
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
    font-size: 17px;
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
    gap: 8px 14px;
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
  .fx-position-meta {
    gap: 8px;
  }
  .fx-position-meta dd {
    font-size: 11px;
  }
  .fx-position-meta dt {
    font-size: 10px;
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
  .fx-spot-pnl > strong,
  .fx-position-heading :deep(.short-pnl-amount) {
    font-size: 20px;
  }
}
</style>
