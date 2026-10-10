<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { compareFxAmounts, computeMinOut, formatFxAmount, multiplyFxAmount, subtractFxAmounts, mapFxError } from '@/api/fx'
import { useFxBorrowBuyQuote } from '@/composables/useFxBorrowBuyQuote'
import type { FxBorrowBuyRequest } from '@/types/fx'

const props = defineProps<{
  pairId: number | null
  currency: string
  active: boolean
  blocked: boolean
  blockedReason?: string
  accountReady: boolean
  resetToken: number
}>()
const emit = defineEmits<{ submit: [body: Omit<FxBorrowBuyRequest, 'idempotency_key'>] }>()
const amount = ref(''), borrowed = ref(''), slippageBps = ref(50), now = ref(Date.now())
const valid = computed(() => [amount.value, borrowed.value].every(v => /^\d+(\.\d{0,6})?$/.test(v.trim())
  && compareFxAmounts(v, '0') === 1 && compareFxAmounts(v, '9999999999.999999') !== 1)
  && compareFxAmounts(borrowed.value, amount.value) !== 1)
const order = computed(() => props.active && props.accountReady && !props.blocked && props.pairId && valid.value
  ? { pairId: props.pairId, body: { amount: amount.value.trim(), borrow_amount: borrowed.value.trim() } } : null)
const { quote, loading, error, refresh } = useFxBorrowBuyQuote(order)
const cashAmount = computed(() => valid.value ? subtractFxAmounts(amount.value, borrowed.value) : null)
const expired = computed(() => !!quote.value && now.value >= Date.parse(quote.value.expires_at))
// A timeout only updates freshness; it never polls the account or quote API.
let expiryTimer: ReturnType<typeof setTimeout> | null = null
watch(quote, q => {
  if (expiryTimer) clearTimeout(expiryTimer)
  now.value = Date.now()
  if (q) expiryTimer = setTimeout(() => { now.value = Date.now() }, Math.max(0, Date.parse(q.expires_at) - Date.now()))
})
watch(() => props.resetToken, () => { amount.value = ''; borrowed.value = '' })
watch(() => props.pairId, () => { amount.value = ''; borrowed.value = '' })
onUnmounted(() => { if (expiryTimer) clearTimeout(expiryTimer) })
const minimum = computed(() => quote.value?.output_amount ? computeMinOut(quote.value.output_amount, slippageBps.value) : '')
const canSubmit = computed(() => !props.blocked && props.accountReady && valid.value && !loading.value
  && quote.value?.executable && !expired.value && !!minimum.value)
const reason = computed(() => quote.value?.blocked_reason
  ? mapFxError({ data: { detail: quote.value.blocked_reason } }, '本笔融资暂不能成交') : '')
function submit() {
  if (!canSubmit.value || !order.value) return
  emit('submit', { ...order.value.body, min_out: minimum.value })
}
function percent(value: string | null | undefined) {
  return value == null ? '—' : `${formatFxAmount(multiplyFxAmount(value, '100'), 2)}%`
}
</script>

<template>
  <div class="financing-form">
    <label>买入总额（含手续费）<small>金圆券</small>
      <input v-model="amount" inputmode="decimal" autocomplete="off" placeholder="0" :disabled="blocked" />
    </label>
    <label>本次新增借款<small>金圆券</small>
      <input v-model="borrowed" inputmode="decimal" autocomplete="off" placeholder="0" :disabled="blocked" />
    </label>
    <p class="note">借款金额需大于 0 且不超过买入总额，差额使用可用现金。借款和买入一起完成。</p>
    <dl>
      <div><dt>使用现金</dt><dd>{{ formatFxAmount(cashAmount) }} 金圆券</dd></div>
      <div><dt>新增借款</dt><dd>{{ valid ? formatFxAmount(borrowed) : '—' }} 金圆券</dd></div>
      <div><dt>预计买入</dt><dd>{{ formatFxAmount(quote?.output_amount) }} {{ currency }}</dd></div>
      <div><dt>手续费（已含）</dt><dd>{{ formatFxAmount(quote?.fee_amount) }} 金圆券</dd></div>
      <div><dt>成交后金圆券债务（含息）</dt><dd>{{ formatFxAmount(quote?.estimated_debt) }}</dd></div>
    </dl>
    <div v-if="quote" class="risk" :class="{ danger: quote.margin_status !== 'healthy' }">
      <strong>预计成交后保证金率 {{ percent(quote.equity_to_risk_basis) }}</strong>
      <span>开仓门槛 ≥ {{ percent(quote.r_initial) }}，强平线 &lt; {{ percent(quote.r_maintenance) }}</span>
      <span>清算净值 {{ formatFxAmount(quote.estimated_equity) }}，风险基数 {{ formatFxAmount(quote.estimated_risk_basis) }}</span>
    </div>
    <p class="note">全账户共同担保，费用和价格影响会减少净值；借款持续计息，保证金不足可能触发强平。</p>
    <p v-if="blockedReason" class="error" role="status">{{ blockedReason }}</p>
    <p v-if="!accountReady" class="error">账户尚未更新，暂不能提交融资交易。</p>
    <p v-else-if="amount && borrowed && !valid" class="error">请输入有效金额，最多 6 位小数，借款不能超过买入总额。</p>
    <p v-if="error || reason" class="error" role="alert">{{ error || reason }}</p>
    <p v-else-if="loading" class="note" role="status">正在计算融资报价与成交后风险…</p>
    <p v-else-if="expired" class="error" role="status">报价已过期，请重新报价。</p>
    <div class="actions">
      <button class="submit" :disabled="!canSubmit" @click="submit">借款并买入</button>
      <button class="refresh" :disabled="!order || loading" @click="refresh">重新报价</button>
    </div>
    <details>
      <summary>授信、利息与成交保护</summary>
      <dl>
        <div><dt>系统名义杠杆上限</dt><dd>{{ formatFxAmount(quote?.leverage, 2) }} 倍</dd></div>
        <div><dt>每日复利率</dt><dd>{{ percent(quote?.daily_rate) }}</dd></div>
        <div><dt>最低买入数量</dt><dd>{{ minimum ? formatFxAmount(minimum) : '—' }} {{ currency }}</dd></div>
      </dl>
      <label>报价变动容忍度<select v-model.number="slippageBps" :disabled="blocked">
        <option :value="50">0.5%</option><option :value="100">1%</option><option :value="200">2%</option><option :value="500">5%</option>
      </select></label>
      <p class="note">名义杠杆是授信配置，本笔资金没有划入独立保证金账户。报价有效期 30 秒，成交时重新检查价格与全账户风险。</p>
    </details>
  </div>
</template>

<style scoped>
.financing-form { display: flex; flex-direction: column; gap: 14px; }
label { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-weight: 700; font-size: 13px; }
label small { margin-left: auto; color: #666; font-weight: 400; }
input, select { box-sizing: border-box; width: 100%; border: 2px solid #111; border-radius: 0; padding: 12px; background: #fff; font: inherit; }
input { font-size: 23px; font-variant-numeric: tabular-nums; }
dl { margin: 0; padding: 14px; background: #f5f5f5; border: 1px solid #ccc; display: grid; gap: 12px; }
dl div { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; font-size: 12px; }
dd { margin: 0; font-weight: 700; overflow-wrap: anywhere; }
.note { margin: 0; color: #666; font-size: 12px; line-height: 1.7; }
.error, .danger { color: #b91c1c; }
.error { margin: 0; font-size: 12px; }
.risk { display: grid; gap: 8px; padding: 14px; border: 2px solid currentColor; font-size: 12px; }
.actions { display: flex; align-items: center; gap: 12px; }
button { border: 0; border-radius: 0; font: inherit; cursor: pointer; }
.submit { flex: 1; padding: 14px; background: #111; color: #fff; font-weight: 700; }
.refresh { padding: 8px 0; background: none; text-decoration: underline; font-size: 12px; }
button:disabled, input:disabled { opacity: .45; cursor: not-allowed; }
details { font-size: 12px; }
summary { cursor: pointer; padding: 10px 0; font-weight: 700; }
details label, details p { margin-top: 12px; }
</style>
