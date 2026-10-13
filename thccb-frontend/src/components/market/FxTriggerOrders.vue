<script setup lang="ts">
import { computed, onUnmounted, readonly, ref, watch } from 'vue'
import { NModal } from 'naive-ui'
import { authIdentityGuard } from '@/api'
import { describeFxOrderFailure, formatFxAmount, formatFxPrice, fxApi, mapFxOrderError } from '@/api/fx'
import { useAuthStore } from '@/stores/auth'
import { useTradingSessionStore } from '@/stores/tradingSession'
import type { FxPairPublic, FxTriggerAction, FxTriggerOrder, FxTriggerStatus } from '@/types/fx'

const props = defineProps<{ pairId: number }>()
const emit = defineEmits<{ 'waiting-change': [orders: readonly FxTriggerOrder[]] }>()
const show = ref(false)
function open() { show.value = true; void load() }
const auth = useAuthStore(), session = useTradingSessionStore()
const pairs = ref<FxPairPublic[]>([]), waiting = ref<FxTriggerOrder[]>([]), records = ref<FxTriggerOrder[]>([])
const action = ref<FxTriggerAction>('buy'), comparison = ref<'lte' | 'gte'>('lte')
const triggerPrice = ref(''), amount = ref(''), closePercent = ref('100'), autoBorrow = ref(false), borrowLimit = ref('')
const resultStatus = ref<Exclude<FxTriggerStatus, 'waiting'>>('succeeded')
const beforeId = ref<number | undefined>(), pageCursors = ref<(number | undefined)[]>([])
const loading = ref(false), submitting = ref(false), waitingLoaded = ref(false)
const loadError = ref(''), formError = ref(''), confirmation = ref('')
const PAGE_SIZE = 20
let generation = 0, mounted = true
const isClose = computed(() => action.value === 'close_long' || action.value === 'close_short')
const currentCurrency = computed(() => currency(props.pairId))
const currentPosition = computed(() => {
  const long = session.account?.fx_wallets.find(row => row.pair_id === props.pairId)?.foreign_amount ?? '0'
  const short = session.account?.fx_shorts.find(row => row.pair_id === props.pairId)
  return { long, short: short?.principal_foreign ?? '0' }
})
function currency(pairId: number) {
  const pair = pairs.value.find(row => row.id === pairId)
  return pair ? `${pair.currency_name} (${pair.currency_code})` : `币种 #${pairId}`
}
const actionNames: Record<FxTriggerAction, string> = { buy: '买入', sell: '卖出', close_long: '平多头', close_short: '平空头' }
const statusNames: Record<FxTriggerStatus, string> = { waiting: '等待触发', succeeded: '已成交', failed: '未成交', cancelled: '已撤销' }
function compact(value: string) { return value.includes('.') ? value.replace(/0+$/, '').replace(/\.$/, '') : value }
function amountText(value: string | null | undefined) { return compact(formatFxAmount(value)) }
function priceText(value: string) { return compact(formatFxPrice(value, { base: 6, max: 6 })) }
function currencyCode(pairId: number) { return pairs.value.find(row => row.id === pairId)?.currency_code ?? `币种 #${pairId}` }
function instruction(order: FxTriggerOrder) {
  return `${actionNames[order.action]} ${order.action.startsWith('close_') ? `${amountText(order.close_percent)}%` : `${amountText(order.amount)} ${order.action === 'buy' ? '金圆券' : currencyCode(order.pair_id)}`}`
}
function date(value: string) {
  const time = new Date(value), pad = (part: number) => String(part).padStart(2, '0')
  return `${pad(time.getMonth() + 1)}-${pad(time.getDate())} ${pad(time.getHours())}:${pad(time.getMinutes())}`
}
async function load() {
  const request = ++generation, valid = authIdentityGuard()
  if (!auth.user) return
  loading.value = true; loadError.value = ''
  try {
    const [active, history, currencies] = await Promise.all([
      fxApi.listTriggerOrders({ status: 'waiting', limit: 100 }),
      fxApi.listTriggerOrders({ status: resultStatus.value, before_id: beforeId.value, limit: PAGE_SIZE }),
      fxApi.listPairs(),
    ])
    if (!mounted || request !== generation || !valid()) return
    waiting.value = active; records.value = history; pairs.value = currencies; waitingLoaded.value = true
  } catch {
    if (mounted && request === generation && valid()) loadError.value = '挂单记录读取失败，请刷新重试。'
  } finally { if (mounted && request === generation && valid()) loading.value = false }
}
function resetPage() { beforeId.value = undefined; pageCursors.value = [] }
function nextPage() {
  pageCursors.value.push(beforeId.value)
  beforeId.value = records.value[records.value.length - 1]?.id
  void load()
}
function previousPage() { beforeId.value = pageCursors.value.pop(); void load() }
async function submit() {
  if (submitting.value || !auth.user || !waitingLoaded.value || waiting.value.length >= 3) return
  const valid = authIdentityGuard(), pairId = props.pairId
  submitting.value = true; formError.value = ''; confirmation.value = ''
  try {
    const order = await fxApi.createTriggerOrder({
      pair_id: pairId, action: action.value, comparison: comparison.value, trigger_price: triggerPrice.value.trim(),
      amount: isClose.value ? null : amount.value.trim(), close_percent: isClose.value ? closePercent.value : null,
      auto_borrow: action.value === 'buy' && autoBorrow.value,
      borrow_limit: action.value === 'buy' && autoBorrow.value ? borrowLimit.value.trim() : '0',
    })
    if (!mounted || !valid()) return
    confirmation.value = `挂单 #${order.id} 已创建，等待触发。`
    resetPage(); await load()
  } catch (error) {
    if (mounted && valid()) formError.value = mapFxOrderError(error, '挂单创建失败，请检查输入后重试')
  } finally { if (mounted && valid()) submitting.value = false }
}
async function cancel(order: FxTriggerOrder) {
  if (submitting.value) return
  const valid = authIdentityGuard()
  submitting.value = true; formError.value = ''; confirmation.value = ''
  try {
    const result = await fxApi.cancelTriggerOrder(order.id)
    if (!mounted || !valid()) return
    confirmation.value = result.status === 'cancelled' ? `挂单 #${order.id} 已撤销。` : `挂单 #${order.id} ${statusNames[result.status]}，请查看记录。`
    await load()
  } catch (error) {
    if (mounted && valid()) formError.value = mapFxOrderError(error, '撤销失败，请刷新挂单记录后重试')
  } finally { if (mounted && valid()) submitting.value = false }
}
watch(waiting, orders => emit('waiting-change', readonly(orders)), { flush: 'sync', immediate: true })
watch(() => auth.sessionGeneration, () => {
  show.value = false
  generation++; waiting.value = []; records.value = []; pairs.value = []; waitingLoaded.value = false
  loading.value = false; submitting.value = false; formError.value = ''; loadError.value = ''; confirmation.value = ''
  triggerPrice.value = ''; amount.value = ''; autoBorrow.value = false; borrowLimit.value = ''; resetPage(); void load()
}, { flush: 'sync', immediate: true })
watch(() => props.pairId, () => { triggerPrice.value = ''; amount.value = ''; confirmation.value = ''; formError.value = '' })
watch(resultStatus, () => { resetPage(); void load() })
watch(() => session.lastOrderResult, result => { if (result) { resetPage(); void load() } })
onUnmounted(() => { mounted = false; generation++; emit('waiting-change', []) })
</script>

<template>
  <button class="manage-orders" type="button" @click="open">管理挂单 <span>{{ waitingLoaded ? waiting.length : '—' }}/3</span></button>
  <NModal v-model:show="show">
  <section class="trigger-orders" role="dialog" aria-modal="true" aria-labelledby="trigger-orders-title">
    <header class="order-heading">
      <h2 id="trigger-orders-title">管理挂单</h2>
      <span>全账户等待单 {{ waitingLoaded ? waiting.length : '—' }}/3</span>
      <button type="button" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新记录' }}</button>
      <button type="button" aria-label="关闭管理挂单" @click="show = false">关闭</button>
    </header>
    <div class="order-body">
    <template v-if="auth.user">
      <form class="order-form" @submit.prevent="submit">
        <div class="fill-sentence"><div class="condition-clause"><span>当 {{ currentCurrency }} 汇率</span><span class="condition-fields">
          <select v-model="comparison" aria-label="触价条件"><option value="lte">小于或等于</option><option value="gte">大于或等于</option></select>
          <input :value="triggerPrice" @input="triggerPrice = ($event.target as HTMLInputElement).value" aria-label="触发汇率" type="text" inputmode="decimal" autocomplete="off" required placeholder="填写触发价" />
          <span>金圆券</span></span></div><div class="action-clause"><span>时，尝试</span><span class="action-fields">
          <select v-model="action" aria-label="挂单动作"><option value="buy">买入</option><option value="sell">卖出</option><option value="close_long">平多头</option><option value="close_short">平空头</option></select>
          <template v-if="!isClose">
            <input :value="amount" @input="amount = ($event.target as HTMLInputElement).value" :aria-label="action === 'buy' ? '投入金圆券' : '卖出外币数量'" type="text" inputmode="decimal" autocomplete="off" required placeholder="填写数量" />
            {{ action === 'buy' ? '金圆券的外币' : '外币' }}。
          </template>
          <template v-else>
            <select v-model="closePercent" aria-label="平仓比例"><option value="25">25%</option><option value="50">50%</option><option value="75">75%</option><option value="100">100%</option></select>
            的{{ action === 'close_long' ? '多头' : '空头' }}仓位。
          </template>
        </span></div></div>
        <p v-if="action === 'sell'" class="order-note">卖出数量超过到价时的持仓，可能借币开空。</p>
        <p v-if="isClose" class="order-note">按到价时仓位计算比例，只减仓，不反向开仓。当前多头 {{ amountText(currentPosition.long) }}，空头本金 {{ amountText(currentPosition.short) }}。</p>
        <div v-if="action === 'buy'" class="borrow-field">
          <label><input v-model="autoBorrow" type="checkbox" /> 允许自动借入金圆券</label>
          <label v-if="autoBorrow">借款上限 <input :value="borrowLimit" @input="borrowLimit = ($event.target as HTMLInputElement).value" aria-label="自动借款上限" type="text" inputmode="decimal" autocomplete="off" required placeholder="填写金圆券上限" /></label>
          <span v-if="autoBorrow" class="order-note">按资金缺口借入，须符合借款条件。</span>
        </div>
        <div class="submit-row"><button class="create-order" :disabled="submitting || !waitingLoaded || waiting.length >= 3" type="submit">{{ submitting ? '处理中…' : '创建挂单' }}</button><span class="order-note">修改请撤销重建。</span></div>
      </form>
      <p v-if="formError" class="order-error" role="alert">{{ formError }}</p>
      <p v-if="confirmation" role="status">{{ confirmation }}</p>
      <p v-if="loadError" class="order-error" role="alert">{{ loadError }}</p>
      <h3>全账户等待单</h3>
      <p v-if="waitingLoaded && !waiting.length" class="order-note">暂无等待挂单。</p>
      <ul class="waiting-list">
        <li v-for="order in waiting" :key="order.id">
          <div class="waiting-content"><div class="row-main"><strong>{{ currencyCode(order.pair_id) }}</strong><span>{{ instruction(order) }}</span></div><p class="row-detail">触发 {{ order.comparison === 'lte' ? '≤' : '≥' }} {{ priceText(order.trigger_price) }}<span v-if="order.auto_borrow">，借款上限 {{ amountText(order.borrow_limit) }} 金圆券</span></p></div>
          <button type="button" :disabled="submitting" @click="cancel(order)">撤销</button>
        </li>
      </ul>
      <details class="history-details"><summary>挂单结果</summary>
      <div class="record-heading"><span>状态</span><select v-model="resultStatus" aria-label="结果状态"><option value="succeeded">已成交</option><option value="failed">未成交</option><option value="cancelled">已撤销</option></select></div>
      <p v-if="!loading && !records.length && !loadError" class="order-note">暂无{{ statusNames[resultStatus] }}记录。</p>
      <ul class="result-list">
        <li v-for="order in records" :key="order.id">
          <div class="row-main"><strong>{{ currencyCode(order.pair_id) }}</strong><span>{{ instruction(order) }}</span><span class="result-state" :class="{ 'order-error': order.status === 'failed' }">{{ statusNames[order.status] }}</span></div>
          <div class="row-detail"><span>触发 {{ order.comparison === 'lte' ? '≤' : '≥' }} {{ priceText(order.trigger_price) }}<template v-if="order.observed_trigger_price">，检查 {{ priceText(order.observed_trigger_price) }}</template></span><time :datetime="order.finished_at ?? order.created_at">{{ date(order.finished_at ?? order.created_at) }}</time></div>
          <p v-if="order.status === 'failed'" class="order-error failure-cause">{{ describeFxOrderFailure(order.failure_reason) }}</p>
        </li>
      </ul>
      <nav class="pagination" aria-label="挂单结果翻页"><button :disabled="loading || !pageCursors.length" @click="previousPage">上一页</button><button :disabled="loading || records.length < PAGE_SIZE" @click="nextPage">下一页</button></nav>
      </details>
    </template>
    <p v-else class="order-note">登录后可创建挂单和查看记录。</p>
    </div>
  </section>
  </NModal>
</template>

<style scoped>
.manage-orders { justify-self: start; font-size: 12px; padding: 7px 12px; }
.manage-orders span { margin-left: 8px; color: #777; font-variant-numeric: tabular-nums; }
.trigger-orders { width: min(560px, calc(100vw - 24px)); max-height: 85dvh; min-width: 0; box-sizing: border-box; display: flex; flex-direction: column; border: 2px solid #000; background: #fff; color: #000; font-size: 12px; }
.order-heading { flex-shrink: 0; padding: 12px; border-bottom: 1px solid #ddd; }
.order-body { min-height: 0; overflow-y: auto; padding: 0 12px 12px; }
.history-details { margin-top: 14px; border-top: 1px solid #ddd; padding-top: 10px; }
.history-details summary { cursor: pointer; font-weight: 700; }
.history-details .record-heading { margin-top: 8px; }
h2, h3, p { margin: 0; }
h2 { font-size: 14px; } h3 { font-size: 12px; margin-top: 18px; margin-bottom: 8px; }
button, input, select { font: inherit; color: inherit; background: #fff; border: 1px solid #000; padding: 5px 8px; max-width: 100%; }
button { cursor: pointer; } button:disabled { opacity: .4; cursor: not-allowed; }
:is(button, input, select, summary):focus-visible { outline: 2px solid #000; outline-offset: 3px; }
.order-heading, .record-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; }
.order-heading > span { color: #777; } .order-heading > button:first-of-type { margin-left: auto; }
.order-form { padding-top: 12px; border-bottom: 1px solid #ddd; padding-bottom: 12px; }
.fill-sentence, .condition-clause, .action-clause, .condition-fields, .action-fields { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.fill-sentence { gap: 8px 12px; line-height: 1.5; } .fill-sentence :is(input, select) { line-height: 1.4; } .fill-sentence input { width: 142px; }
.order-note { color: #777; line-height: 1.7; } .order-form > .order-note { margin-top: 8px; }
.borrow-field { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 16px; margin-top: 10px; }
.borrow-field label { display: flex; align-items: center; gap: 6px; } .borrow-field input[type=text] { width: 142px; }
.submit-row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 12px; }
.create-order { background: #000; color: #fff; font-weight: 700; padding: 7px 16px; }
.order-error { color: var(--color-down, #b52819); line-height: 1.7; }
.order-body > p { margin-top: 10px; } ul { margin: 0; padding: 0; list-style: none; }
li { border-bottom: 1px solid #eee; padding: 8px 0; line-height: 1.5; overflow-wrap: anywhere; }
.waiting-list li { display: flex; justify-content: space-between; align-items: center; gap: 14px; }
.waiting-list button { flex-shrink: 0; } small { color: #777; font-size: 11px; }
.record-heading { margin-top: 18px; } .record-heading h3 { margin: 0; } .record-heading select { margin-left: auto; }
.waiting-content { min-width: 0; }
.row-main { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 8px; font-variant-numeric: tabular-nums; }
.row-detail { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 12px; margin-top: 3px; color: #777; font-size: 11px; font-variant-numeric: tabular-nums; }
.result-state { margin-left: auto; color: #777; white-space: nowrap; } .result-state.order-error { color: var(--color-down, #b52819); }
.row-detail time { margin-left: auto; white-space: nowrap; } .failure-cause { margin-top: 3px; font-size: 11px; }
.pagination { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
@media (max-width: 640px) {
  .order-heading { padding: 10px; }
  .order-body { padding: 0 10px 10px; }
  button, select, input[type=text] { min-height: 36px; box-sizing: border-box; }
  input[type=text] { font-size: 16px; }
  .fill-sentence { display: grid; gap: 8px; }
  .condition-clause { display: grid; gap: 6px; }
  .condition-fields { display: grid; grid-template-columns: 104px minmax(0, 1fr) auto; gap: 6px; }
  .condition-fields input { width: 100%; min-width: 0; }
  .action-clause { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 6px; }
  .action-fields { display: flex; flex-wrap: nowrap; min-width: 0; gap: 6px; }
  .action-fields input { width: 100%; min-width: 0; flex: 1; }
  .action-fields select { max-width: 84px; flex-shrink: 0; }
  .borrow-field { gap: 6px; margin-top: 8px; }
  .borrow-field > label:last-of-type { flex-wrap: wrap; }
  .order-heading { gap: 6px; } .order-heading > span { font-size: 11px; }
  .order-heading > button { padding-inline: 6px; }
  .submit-row { margin-top: 8px; }
  .record-heading { margin-top: 14px; }
  h3 { margin-top: 14px; }
  .waiting-list li { gap: 8px; }
  .waiting-list button { padding-inline: 9px; }
}
</style>
