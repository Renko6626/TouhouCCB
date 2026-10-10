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
  tradeSlippageBps,
} from '@/api/fx'
import type { FxPendingShortRequest } from '@/api/fx'
import { userApi } from '@/api/user'
import { useAuthStore } from '@/stores/auth'
import type { UserSummary } from '@/types/user'
import { useFxResource } from '@/composables/useFxResource'
import { useFxShortQuote, type ShortQuoteOrder } from '@/composables/useFxShortQuote'
import {
  fxAvailableCash,
  fxBuyBlockReason,
  fxFundingPreview,
  fxGoldPerForeign,
  fxHoldingValue,
  fxPairAllowsSide,
  fxSellAllocation,
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
    !pendingShort.value &&
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
const quotePriceImpact = computed(() =>
  tradeSlippageBps(quoteReferencePrice.value, effectiveGoldPerForeign.value),
)
const holdingValue = computed(() => fxHoldingValue(wallet.value, snapshot.value?.price))
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
const canFillBuy = computed(
  () =>
    side.value === 'buy' &&
    tradable.value &&
    !submitting.value &&
    !summaryLoading.value &&
    !summaryFailed.value &&
    !!summary.value &&
    !fxBuyBlockReason(summary.value, ''),
)
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
if (pendingShort.value) tradeMode.value = 'short'
const shortStorageBlocked = ref(shortSubmitter.unreadable || !authStore.user?.id)
watch(
  () => authStore.user?.id,
  (id) => {
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
    pendingShort.value ||
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
} = useFxShortQuote(shortQuoteOrder)
const shortGoldPresets = ['100', '500', '1000']
const canFillShortOpen = computed(
  () =>
    !submitting.value &&
    !pendingShort.value &&
    !shortStorageBlocked.value &&
    !snapshotLoading.value &&
    !snapshotFailed.value &&
    snapshot.value?.pair.id === pairId.value,
)
const canFillCoverAll = computed(
  () => !submitting.value && !pendingShort.value && !shortStorageBlocked.value,
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
function fillShortOpen(gold: string) {
  if (!canFillShortOpen.value) return
  shortSizeUnit.value = 'gold'
  updateShortGoldAmount(gold)
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
function onShortGoldInput(event: Event) {
  updateShortGoldAmount((event.target as HTMLInputElement).value)
}
function setShortSizeUnit(unit: 'gold' | 'foreign') {
  if (submitting.value || pendingShort.value) return
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
    !pendingShort.value &&
    !shortStorageBlocked.value,
)
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
const canCloseShortHolding = computed(() => canFillCover.value && hasShortHolding.value)
const fundingPreview = computed(() =>
  tradeMode.value === 'spot' &&
  side.value === 'buy' &&
  !summaryLoading.value &&
  !summaryFailed.value
    ? fxFundingPreview(amount.value, summary.value)
    : null,
)
const fundingNeeded = computed(() => compareFxAmounts(fundingPreview.value?.shortfall, '0') === 1)
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
}
const accountRiskLabel = computed(() => riskLabels[accountRisk.value])
const quotedShortRiskLabel = computed(() =>
  shortQuote.value
    ? riskLabels[
        resolveCreditRiskStatus({
          authoritativeStatus: shortQuote.value.margin_status,
          ratio: shortPostMargin.value,
          noRisk: compareFxAmounts(shortQuote.value.estimated_risk_basis, '0') === 0,
        })
      ]
    : '等待报价',
)

function selectDirection(mode: 'spot' | 'short') {
  if (submitting.value || pendingShort.value) return
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
  const generation = ++shortReadGeneration
  shortLoading.value = true
  try {
    const row = await fxApi.getShort(pid)
    if (pid === pairId.value && generation === shortReadGeneration) {
      shortPosition.value = row
      shortFailed.value = false
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
async function submitShort() {
  if (submitting.value || pendingShort.value) return
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

watch([amount, side, pairId], scheduleQuote, { flush: 'sync' })

onMounted(() => {
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

    <div v-if="loading" class="fx-state">行情加载中…</div>
    <div v-else-if="error" class="fx-state fx-state-error">
      {{ error }}<button class="btn-secondary" @click="load">重试</button>
    </div>
    <div v-else-if="!activePair" class="fx-state">外汇交易暂未开放，请稍后再来查看。</div>

    <template v-else>
      <div class="fx-account-bar" aria-label="账户概览">
        <span>我的账户</span>
        <div>
          净值
          <strong>{{
            summaryLoading
              ? '刷新中…'
              : summaryFailed
                ? '更新失败'
                : formatFxAmount(summary?.display_equity, 2)
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
        <small>金额单位：金圆券</small>
      </div>
      <div v-if="activePair.reduce_only" class="fx-notice">
        当前币种仅可减仓，可从持仓选择平仓。
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

      <div class="fx-workbench">
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
                height="100%"
              />
            </div>
            <div class="fx-chart-footer">
              <span>上涨有利于多头，下跌有利于空头</span><span>金圆券 / {{ currencyName }}</span>
            </div>
          </section>

          <section class="fx-holdings" aria-labelledby="fx-holdings-title">
            <div class="fx-panel-head">
              <h2 id="fx-holdings-title">我的 {{ currencyName }} 持仓</h2>
              <span class="fx-hint">查看盈亏、减仓和平仓</span>
            </div>
            <p v-if="walletLoading || shortLoading" class="fx-hint" role="status">
              正在刷新持仓，已有数字为上次快照…
            </p>
            <p v-if="walletFailed" class="fx-error" role="alert">
              多头持仓更新失败。<button class="fx-text-button" @click="loadWallet">重新加载</button>
            </p>
            <p v-if="shortFailed" class="fx-error" role="alert">
              空头持仓更新失败。<button class="fx-text-button" @click="loadShort">重新加载</button>
              <button
                class="fx-text-button"
                :disabled="!canFillCoverAll"
                @click="prepareShortCover(100)"
              >
                查看全部平仓报价
              </button>
            </p>
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
              <div class="fx-position-bottom">
                <span>卖出持仓即可平仓，有借款时所得优先还款。</span>
                <div>
                  <button
                    class="btn-secondary"
                    :disabled="!canSellSpotHolding"
                    @click="prepareSpotSellAll(50)"
                  >
                    减仓 50%</button
                  ><button
                    class="fx-black-button"
                    :disabled="!canSellSpotHolding"
                    @click="prepareSpotSellAll(100)"
                  >
                    全部平仓
                  </button>
                </div>
              </div>
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
              <div class="fx-position-bottom">
                <span>买回归还即可平仓，结清后释放剩余锁金。</span>
                <div>
                  <button
                    class="btn-secondary"
                    :disabled="!canCloseShortHolding"
                    @click="prepareShortCover(50)"
                  >
                    减仓 50%</button
                  ><button
                    class="fx-black-button"
                    :disabled="!canFillCoverAll"
                    @click="prepareShortCover(100)"
                  >
                    全部平仓
                  </button>
                </div>
              </div>
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
              空头欠币暂无法估值，可尝试获取全部平仓报价。<button
                class="fx-text-button"
                :disabled="!canFillCoverAll"
                @click="prepareShortCover(100)"
              >
                查看平仓报价
              </button>
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
                <p>选择看涨或看跌，成交后在这里查看盈亏和平仓。</p>
              </div>
            </div>
          </section>
        </div>

        <aside class="fx-trade-panel" aria-labelledby="fx-trade-heading">
          <div class="fx-order-heading">
            <h2 id="fx-trade-heading" ref="tradePanelRef" class="fx-trade-heading" tabindex="-1">
              {{ isClosing ? '平仓' : '开仓' }}
            </h2>
            <span>交易 {{ currencyName }}</span>
          </div>
          <div class="fx-direction-picker" role="group" aria-label="选择涨跌方向">
            <button
              class="fx-direction-long"
              :class="{ active: tradeMode === 'spot' }"
              :aria-pressed="tradeMode === 'spot'"
              :disabled="submitting || !!pendingShort"
              @click="selectDirection('spot')"
            >
              <span aria-hidden="true">↗</span><strong>看涨做多</strong><small>Long</small>
            </button>
            <button
              class="fx-direction-short"
              :class="{ active: tradeMode === 'short' }"
              :aria-pressed="tradeMode === 'short'"
              :disabled="submitting || !!pendingShort"
              @click="selectDirection('short')"
            >
              <span aria-hidden="true">↘</span><strong>看跌做空</strong><small>Short</small>
            </button>
          </div>
          <div v-if="isClosing" class="fx-close-context">
            <strong>{{ tradeMode === 'spot' ? '卖出多头持仓' : '买回归还空头' }}</strong
            ><button
              class="fx-text-button"
              :disabled="submitting || !!pendingShort"
              @click="selectDirection(tradeMode)"
            >
              返回开仓
            </button>
          </div>
          <p v-else class="fx-direction-help">
            {{
              tradeMode === 'spot'
                ? `买入${currencyName}，等待上涨后卖出。`
                : `借入${currencyName}卖出，等待下跌后买回。`
            }}
          </p>

          <div v-show="tradeMode === 'spot'" class="fx-trade-body">
            <label class="fx-field"
              ><span
                >{{ side === 'buy' ? '买入金额（含手续费）' : '平仓数量' }}
                <small>{{ inputCurrency }}</small></span
              ><input
                v-model="amount"
                class="fx-input fx-size-input"
                inputmode="decimal"
                autocomplete="off"
                placeholder="0"
                :disabled="!tradable || submitting || !!pendingShort"
            /></label>
            <div class="fx-sell-shortcuts">
              <button
                v-for="percent in portions"
                :key="percent"
                class="btn-secondary"
                :disabled="side === 'buy' ? !canFillBuy : !canFillSell"
                @click="side === 'buy' ? fillBuyPortion(percent) : fillSellPortion(percent)"
              >
                {{ percent === 100 ? '全部' : `${percent}%` }}
              </button>
            </div>
            <p class="fx-field-note">
              {{
                side === 'buy'
                  ? '按可用现金比例填写，可先借款扩大买入规模。'
                  : '按当前多头持仓比例填写。'
              }}
            </p>
            <div v-if="side === 'buy'" class="fx-funding-preview">
              <div>
                <span>现金投入</span
                ><strong>{{ formatFxAmount(fundingPreview?.cashInput, 2) }}</strong>
              </div>
              <b>+</b>
              <div>
                <span>还需资金</span
                ><strong>{{ formatFxAmount(fundingPreview?.shortfall, 2) }}</strong>
              </div>
              <b>=</b>
              <div>
                <span>买入金额</span
                ><strong>{{ amountValid ? formatFxAmount(amount, 2) : '—' }}</strong>
              </div>
            </div>
            <div v-if="side === 'buy' && summary" class="fx-credit-entry">
              <div>
                <span>借款可扩大交易规模</span
                ><small>{{
                  summaryLoading || summaryFailed
                    ? '杠杆规则待恢复'
                    : summary.credit_leverage == null
                      ? '额度由账户风控决定'
                      : `账户名义杠杆上限 ${summary.credit_leverage}x`
                }}</small>
              </div>
              <router-link to="/loan">查看借款</router-link>
            </div>
            <p v-if="fundingNeeded" class="fx-hint">
              还需 {{ formatFxAmount(fundingPreview?.shortfall) }} 金圆券。{{
                summary ? '请先借款，再确认做多；现金差额不等于可借额度。' : '请补充现金后再做多。'
              }}
            </p>
            <p v-if="summaryFailed" class="fx-error" role="alert">
              账户更新失败，暂不能做多。<button
                class="fx-text-button"
                :disabled="summaryLoading"
                @click="loadSummary"
              >
                重新加载
              </button>
            </p>
            <div class="fx-preview fx-order-summary">
              <div class="fx-preview-row">
                <span>{{ side === 'buy' ? '预计买入' : '预计卖出所得' }}</span
                ><strong
                  >{{ quote ? formatFxAmount(quote.output_amount) : '—' }}
                  {{ outputCurrency }}</strong
                >
              </div>
              <div class="fx-preview-row">
                <span>手续费（已含）</span
                ><strong
                  >{{ quote ? formatFxAmount(quote.fee_amount) : '—' }} {{ inputCurrency }}</strong
                >
              </div>
              <div v-if="side === 'buy'" class="fx-preview-row">
                <span>当前账户风险</span
                ><strong
                  :class="{ 'fx-error': ['warning', 'danger', 'blocked'].includes(accountRisk) }"
                  >{{ accountRiskLabel }}</strong
                >
              </div>
            </div>
            <div v-if="orderMove && side === 'buy'" class="fx-payoff-preview">
              <span>本笔汇率变化 1%</span>
              <div>
                <span
                  >上涨 1% <strong class="up">+{{ formatFxAmount(orderMove, 2) }}</strong></span
                ><span
                  >下跌 1% <strong class="down">−{{ formatFxAmount(orderMove, 2) }}</strong></span
                >
              </div>
              <small>价格盈亏示意，未扣费用与利息</small>
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
            <p v-if="side === 'buy' && summary" class="fx-shared-risk">
              全账户共同担保，亏损可能影响其他持仓。
            </p>
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
                  !!pendingShort ||
                  !amountValid ||
                  !!tradeBlockReason ||
                  quoting ||
                  !quote ||
                  quoteExpired
                "
                @click="submitTrade"
              >
                {{ submitting ? '提交中…' : side === 'buy' ? '确认做多' : '确认多头平仓' }}</button
              ><button
                class="fx-text-button"
                :disabled="quoting || submitting || !tradable || !amountValid || !!pendingShort"
                @click="fetchQuote"
              >
                重新报价
              </button>
            </div>
            <details class="fx-details fx-order-details">
              <summary>费用、报价与风控明细</summary>
              <div class="fx-preview-row">
                <span>参考{{ side === 'buy' ? '买入' : '卖出' }}价</span
                ><strong>{{ formatFxPrice(sidePrice) }} 金圆券 / {{ currencyName }}</strong>
              </div>
              <div class="fx-preview-row">
                <span>本笔成交均价</span
                ><strong>{{ formatFxPrice(effectiveGoldPerForeign) }}</strong>
              </div>
              <div class="fx-preview-row">
                <span>价格影响（含手续费）</span
                ><strong>{{
                  quotePriceImpact === null ? '—' : (quotePriceImpact / 100).toFixed(2) + '%'
                }}</strong>
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
              <div class="fx-size-label">
                <label :for="shortSizeUnit === 'gold' ? 'fx-short-gold' : 'fx-short-foreign'">{{
                  shortSizeUnit === 'gold' ? '做空参考规模' : '借入并卖出数量'
                }}</label
                ><select
                  :value="shortSizeUnit"
                  aria-label="做空金额单位"
                  :disabled="submitting || !!pendingShort"
                  @change="
                    setShortSizeUnit(
                      ($event.target as HTMLSelectElement).value as 'gold' | 'foreign',
                    )
                  "
                >
                  <option value="gold">金圆券</option>
                  <option value="foreign">{{ currencyName }}</option>
                </select>
              </div>
              <input
                v-if="shortSizeUnit === 'gold'"
                id="fx-short-gold"
                :value="shortGoldAmount"
                class="fx-input fx-size-input"
                inputmode="decimal"
                autocomplete="off"
                placeholder="0"
                :disabled="!canFillShortOpen"
                @input="onShortGoldInput"
              />
              <input
                v-else
                id="fx-short-foreign"
                v-model="shortAmount"
                class="fx-input fx-size-input"
                inputmode="decimal"
                autocomplete="off"
                placeholder="0"
                :disabled="submitting || !!pendingShort || shortStorageBlocked"
              />
              <div class="fx-short-presets">
                <button
                  v-for="gold in shortGoldPresets"
                  :key="gold"
                  class="btn-secondary"
                  :disabled="!canFillShortOpen"
                  @click="fillShortOpen(gold)"
                >
                  {{ gold }} 金圆券
                </button>
              </div>
              <p class="fx-field-note">
                {{
                  shortSizeUnit === 'gold'
                    ? `按输入时汇率换算，借入 ${shortAmount ? formatFxAmount(shortAmount) : '—'} ${currencyName}；此金额不是保证金。`
                    : '外币借入后立即卖出，实际所得以报价为准。'
                }}
              </p>
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
                  :disabled="submitting || !!pendingShort || coverAll"
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
                  :disabled="submitting || !!pendingShort"
                />全部归还，包含成交时新计利息</label
              >
              <p v-if="shortFailed" class="fx-error">持仓更新失败，可选择全部平仓获取最新报价。</p>
            </div>
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
            <div v-if="orderMove && shortAction === 'open'" class="fx-payoff-preview">
              <span>本笔汇率变化 1%</span>
              <div>
                <span
                  >上涨 1% <strong class="down">−{{ formatFxAmount(orderMove, 2) }}</strong></span
                ><span
                  >下跌 1% <strong class="up">+{{ formatFxAmount(orderMove, 2) }}</strong></span
                >
              </div>
              <small>价格盈亏示意，未扣费用与利息</small>
            </div>
            <div class="fx-preview fx-order-summary">
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
            <p class="fx-shared-risk">全账户共同担保，亏损可能影响其他持仓。</p>
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
                {{
                  submitting ? '提交中…' : shortAction === 'open' ? '借币并做空' : '确认空头平仓'
                }}</button
              ><button
                class="fx-text-button"
                :disabled="
                  !shortValid || shortQuoting || submitting || !!pendingShort || shortStorageBlocked
                "
                @click="fetchShortQuote"
              >
                重新报价
              </button>
            </div>
            <details class="fx-details fx-order-details">
              <summary>费用、利息与风控明细</summary>
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
                      t.purpose === 'short_open'
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
      :disabled="submitting || !!pendingShort"
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
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.fx-page button {
  cursor: pointer;
}
.fx-page button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
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
  border: 1px solid #000;
  background: #fff;
  color: #000;
  font-size: 12px;
  padding: 6px 12px;
  min-height: 32px;
  font-weight: 650 !important;
}
.fx-black-button {
  background: #000;
  color: #fff;
}
.fx-topbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 14px 28px;
  padding: 4px 0 20px;
}
.fx-topbar-id {
  display: flex;
  flex-direction: column;
  gap: 10px;
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
  font-size: 32px;
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
  padding-bottom: 15px;
}
.fx-freshness-bar > button {
  margin-left: auto;
}
.fx-account-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 22px;
  background: #f7f7f7;
  border: 1px solid #ddd;
  padding: 12px 14px;
  margin-bottom: 22px;
  font-size: 12px;
}
.fx-account-bar > span {
  font-weight: 650;
}
.fx-account-bar > div {
  color: #777;
}
.fx-account-bar strong {
  color: #000;
  margin-left: 7px;
}
.fx-account-bar strong.fx-error {
  color: var(--color-down);
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
  grid-template-columns: minmax(0, 1fr) 360px;
  gap: 28px;
  align-items: start;
}
.fx-market-column {
  min-width: 0;
}
.fx-chart-panel {
  border-bottom: 1px solid #ddd;
  padding-bottom: 14px;
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
  height: 360px;
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
  padding: 22px 0 0;
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
  font-size: 23px;
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
  font-size: 23px;
  line-height: 1.4;
  margin: 0;
  font-weight: 650;
}
.fx-position-heading :deep(.short-pnl-note) {
  display: none;
}
.fx-position-meta {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  padding: 16px 0;
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
}
.fx-position-bottom > span {
  color: #888;
  font-size: 11px;
  max-width: 50%;
}
.fx-position-bottom > div {
  display: flex;
  gap: 7px;
}
.fx-position-bottom button {
  white-space: nowrap;
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
  border: 1.5px solid #000;
  padding: 20px;
}
.fx-order-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 18px;
  gap: 12px;
}
.fx-trade-heading {
  margin: 0;
  font-size: 17px;
  font-weight: 800;
  scroll-margin-top: 85px;
}
.fx-order-heading > span {
  color: #777;
  font-size: 12px;
}
.fx-direction-picker {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.fx-direction-picker > button {
  display: grid;
  grid-template-columns: 20px 1fr;
  column-gap: 5px;
  text-align: left;
  align-items: center;
  border: 1px solid #ddd;
  padding: 12px;
  background: #fff;
  color: #000;
}
.fx-direction-picker strong {
  font-size: 14px;
  white-space: nowrap;
}
.fx-direction-picker small {
  grid-column: 2;
  color: #888;
  font-size: 10px;
  margin-top: 2px;
}
.fx-direction-picker button > span {
  grid-row: span 2;
  font-size: 21px;
  font-weight: 750;
  align-self: start;
  line-height: 1.3;
}
.fx-direction-long.active {
  background: var(--color-up-bg);
  color: var(--color-up);
  border: 1.5px solid var(--color-up);
}
.fx-direction-short.active {
  background: var(--color-down-bg);
  color: var(--color-down);
  border: 1.5px solid var(--color-down);
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
  padding: 10px;
  margin: 12px 0 18px;
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
  border: 1px solid #bbb;
  padding: 9px 10px;
  background: #fff;
  color: #000;
}
.fx-size-input {
  font-size: 25px !important;
  font-weight: 650 !important;
  min-height: 52px;
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
  border-color: #ddd;
  padding: 5px 0;
  font-size: 11px !important;
  color: #666;
  min-height: 30px;
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
  gap: 4px;
  padding: 13px 10px;
  background: #f5f5f5;
  margin: 16px 0 12px;
}
.fx-funding-preview > div {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
}
.fx-funding-preview span {
  color: #777;
  font-size: 10px;
}
.fx-funding-preview strong {
  font-size: 15px;
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
  border-top: 1px solid #eee;
  padding-top: 15px;
  margin: 16px 0 4px;
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
  padding: 12px;
  background: #f5f5f5;
  margin-top: 16px;
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
  flex-direction: column;
  align-items: stretch;
  gap: 8px;
  margin-top: 8px;
}
.fx-submit {
  display: block;
  width: 100%;
  background: #000;
  color: #fff;
  padding: 12px;
  border: 1px solid #000;
  font-size: 14px !important;
  font-weight: 750 !important;
  min-height: 46px;
}
.fx-actions > .fx-text-button {
  align-self: center;
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
  padding-top: 12px;
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
    padding: 17px;
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
  .fx-chart-body {
    height: 340px;
  }
}
@media (max-width: 950px) {
  .fx-workbench {
    display: flex;
    flex-direction: column;
    gap: 24px;
  }
  .fx-trade-panel {
    width: 100%;
    order: -1;
  }
  .fx-market-column {
    width: 100%;
  }
  .fx-market-volume {
    display: none;
  }
  .fx-connection {
    margin-left: auto;
  }
  .fx-chart-body {
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
  .fx-account-bar > div:last-of-type {
    flex-basis: 100%;
  }
  .fx-trade-panel {
    padding: 17px;
  }
  .fx-direction-picker > button {
    padding: 12px;
    min-height: 63px;
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
    min-height: 48px;
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
