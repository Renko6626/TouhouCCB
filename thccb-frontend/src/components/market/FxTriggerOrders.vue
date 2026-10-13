<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { authIdentityGuard } from '@/api'
import { describeFxOrderFailure, formatFxAmount, formatFxPrice, fxApi, mapFxOrderError } from '@/api/fx'
import { useAuthStore } from '@/stores/auth'
import { useTradingSessionStore } from '@/stores/tradingSession'
import type { FxPairPublic, FxTriggerAction, FxTriggerOrder, FxTriggerStatus } from '@/types/fx'

const props = defineProps<{ pairId: number }>()
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
function instruction(order: FxTriggerOrder) {
  return `${actionNames[order.action]} ${order.action.startsWith('close_') ? `${formatFxAmount(order.close_percent ?? '0')}%` : `${formatFxAmount(order.amount ?? '0')} ${order.action === 'buy' ? '金圆券' : currency(order.pair_id)}`}`
}
function date(value: string) { return new Date(value).toLocaleString('zh-CN', { hour12: false }) }
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
watch(() => auth.sessionGeneration, () => {
  generation++; waiting.value = []; records.value = []; pairs.value = []; waitingLoaded.value = false
  loading.value = false; submitting.value = false; formError.value = ''; loadError.value = ''; confirmation.value = ''
  triggerPrice.value = ''; amount.value = ''; autoBorrow.value = false; borrowLimit.value = ''; resetPage(); void load()
}, { flush: 'sync', immediate: true })
watch(() => props.pairId, () => { triggerPrice.value = ''; amount.value = ''; confirmation.value = ''; formError.value = '' })
watch(resultStatus, () => { resetPage(); void load() })
watch(() => session.lastOrderResult, result => { if (result) { resetPage(); void load() } })
onUnmounted(() => { mounted = false; generation++ })
</script>

<template>
  <section class="trigger-orders" aria-labelledby="trigger-orders-title">
    <header class="order-heading">
      <h2 id="trigger-orders-title">到价挂单</h2>
      <span>全账户等待单 {{ waitingLoaded ? waiting.length : '—' }}/3</span>
      <button type="button" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新记录' }}</button>
    </header>
    <template v-if="auth.user">
      <form class="order-form" @submit.prevent="submit">
        <p class="fill-sentence">当 {{ currentCurrency }} 的汇率
          <select v-model="comparison" aria-label="触价条件"><option value="lte">小于或等于</option><option value="gte">大于或等于</option></select>
          <input :value="triggerPrice" @input="triggerPrice = ($event.target as HTMLInputElement).value" aria-label="触发汇率" type="number" inputmode="decimal" min="0.000001" step="0.000001" required placeholder="填写触发价" />
          金圆券时，尝试
          <select v-model="action" aria-label="挂单动作"><option value="buy">买入</option><option value="sell">卖出</option><option value="close_long">平多头</option><option value="close_short">平空头</option></select>
          <template v-if="!isClose">
            <input :value="amount" @input="amount = ($event.target as HTMLInputElement).value" :aria-label="action === 'buy' ? '投入金圆券' : '卖出外币数量'" type="number" inputmode="decimal" min="0.000001" step="0.000001" required placeholder="填写数量" />
            {{ action === 'buy' ? '金圆券的外币' : '外币' }}。
          </template>
          <template v-else>
            <select v-model="closePercent" aria-label="平仓比例"><option value="25">25%</option><option value="50">50%</option><option value="75">75%</option><option value="100">100%</option></select>
            的{{ action === 'close_long' ? '多头' : '空头' }}仓位。
          </template>
        </p>
        <p v-if="action === 'sell'" class="order-note">卖出数量超过到价时的持仓，可能借币开空。</p>
        <p v-if="isClose" class="order-note">按到价时仓位计算比例，只减仓，不反向开仓。当前多头 {{ formatFxAmount(currentPosition.long) }}，空头本金 {{ formatFxAmount(currentPosition.short) }}。</p>
        <div v-if="action === 'buy'" class="borrow-field">
          <label><input v-model="autoBorrow" type="checkbox" /> 允许自动借入金圆券</label>
          <label v-if="autoBorrow">借款上限 <input :value="borrowLimit" @input="borrowLimit = ($event.target as HTMLInputElement).value" aria-label="自动借款上限" type="number" inputmode="decimal" min="0" step="0.000001" required placeholder="填写金圆券上限" /></label>
          <span v-if="autoBorrow" class="order-note">按资金缺口借入，仍需符合账户借款条件。</span>
        </div>
        <p class="order-note">触发价不保证成交均价；不预留资金或借款额度。定时检查，短暂触价可能错过。到价后尝试一次，失败会结束挂单。</p>
        <div class="submit-row"><button class="create-order" :disabled="submitting || !waitingLoaded || waiting.length >= 3" type="submit">{{ submitting ? '处理中…' : '创建挂单' }}</button><span class="order-note">修改挂单请先撤销，再重新创建。</span></div>
      </form>
      <p v-if="formError" class="order-error" role="alert">{{ formError }}</p>
      <p v-if="confirmation" role="status">{{ confirmation }}</p>
      <p v-if="loadError" class="order-error" role="alert">{{ loadError }}</p>
      <h3>全账户等待单</h3>
      <p v-if="waitingLoaded && !waiting.length" class="order-note">暂无等待挂单。</p>
      <ul class="waiting-list">
        <li v-for="order in waiting" :key="order.id">
          <div><strong>#{{ order.id }} {{ currency(order.pair_id) }}</strong><p>汇率 {{ order.comparison === 'lte' ? '≤' : '≥' }} {{ formatFxPrice(order.trigger_price) }} 时，{{ instruction(order) }}</p><small v-if="order.auto_borrow">可自动借款，上限 {{ formatFxAmount(order.borrow_limit) }} 金圆券</small></div>
          <button type="button" :disabled="submitting" @click="cancel(order)">撤销</button>
        </li>
      </ul>
      <div class="record-heading"><h3>挂单结果</h3><select v-model="resultStatus" aria-label="结果状态"><option value="succeeded">已成交</option><option value="failed">未成交</option><option value="cancelled">已撤销</option></select></div>
      <p v-if="!loading && !records.length && !loadError" class="order-note">暂无{{ statusNames[resultStatus] }}记录。</p>
      <ul class="result-list">
        <li v-for="order in records" :key="order.id">
          <div><strong>#{{ order.id }} {{ currency(order.pair_id) }}</strong><span>{{ statusNames[order.status] }}</span></div>
          <p>{{ instruction(order) }}，触发价 {{ order.comparison === 'lte' ? '≤' : '≥' }} {{ formatFxPrice(order.trigger_price) }}<template v-if="order.observed_trigger_price">，检查时汇率 {{ formatFxPrice(order.observed_trigger_price) }}</template></p>
          <p v-if="order.status === 'failed'" class="order-error">{{ describeFxOrderFailure(order.failure_reason) }}</p>
          <p v-if="order.status === 'succeeded'" class="order-note">{{ order.execution_id ? `成交编号 ${order.execution_id}` : `交易编号 ${order.trade_id}` }}，实际投入与所得可在成交记录查看。</p>
          <small>{{ date(order.finished_at ?? order.created_at) }}</small>
        </li>
      </ul>
      <nav class="pagination" aria-label="挂单结果翻页"><button :disabled="loading || !pageCursors.length" @click="previousPage">上一页</button><button :disabled="loading || records.length < PAGE_SIZE" @click="nextPage">下一页</button></nav>
    </template>
    <p v-else class="order-note">登录后可创建挂单和查看记录。</p>
  </section>
</template>

<style scoped>
.trigger-orders { min-width: 0; border: 1px solid #000; padding: 16px; color: #000; font-size: 12px; }
h2, h3, p { margin: 0; }
h2 { font-size: 14px; } h3 { font-size: 12px; margin-top: 18px; margin-bottom: 8px; }
button, input, select { font: inherit; color: inherit; background: #fff; border: 1px solid #000; padding: 5px 8px; max-width: 100%; }
button { cursor: pointer; } button:disabled { opacity: .4; cursor: not-allowed; }
:is(button, input, select):focus-visible { outline: 2px solid #000; outline-offset: 3px; }
.order-heading, .record-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; }
.order-heading > span { color: #777; } .order-heading > button { margin-left: auto; }
.order-form { padding-top: 16px; border-bottom: 1px solid #ddd; padding-bottom: 16px; }
.fill-sentence { line-height: 2.9; } .fill-sentence :is(input, select) { margin: 0 4px; line-height: 1.4; } .fill-sentence input { width: 142px; }
.order-note { color: #777; line-height: 1.7; } .order-form > .order-note { margin-top: 8px; }
.borrow-field { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 16px; margin-top: 10px; }
.borrow-field label { display: flex; align-items: center; gap: 6px; } .borrow-field input[type=number] { width: 142px; }
.submit-row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 12px; }
.create-order { background: #000; color: #fff; font-weight: 700; padding: 7px 16px; }
.order-error { color: var(--color-down, #b52819); line-height: 1.7; }
.trigger-orders > p { margin-top: 10px; } ul { margin: 0; padding: 0; list-style: none; }
li { border-bottom: 1px solid #eee; padding: 10px 0; line-height: 1.7; overflow-wrap: anywhere; }
.waiting-list li { display: flex; justify-content: space-between; align-items: center; gap: 14px; }
.waiting-list button { flex-shrink: 0; } small { color: #777; font-size: 11px; }
.record-heading { margin-top: 18px; } .record-heading h3 { margin: 0; } .record-heading select { margin-left: auto; }
.result-list li > div { display: flex; justify-content: space-between; gap: 12px; } .result-list li > div > span { color: #777; }
.pagination { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
@media (max-width: 640px) { .trigger-orders { padding: 12px; } .fill-sentence { line-height: 3.7; } button, select, input[type=number] { min-height: 40px; box-sizing: border-box; } input[type=number] { font-size: 16px; } .borrow-field > label:last-of-type { flex-wrap: wrap; } .order-heading { gap: 8px; } .order-heading > span { font-size: 11px; } }
</style>
