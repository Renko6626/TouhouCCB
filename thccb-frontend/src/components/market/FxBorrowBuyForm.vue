<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import {
  compareFxAmounts,
  computeMinOut,
  formatFxAmount,
  multiplyFxAmount,
  subtractFxAmounts,
  mapFxError,
} from '@/api/fx'
import { useFxBorrowBuyQuote } from '@/composables/useFxBorrowBuyQuote'
import { fxGoldPerForeign, fxOrderAmountLabel } from '@/utils/fxPresentation'
import FxExecutionPrice from './FxExecutionPrice.vue'
import FxPostTradeMargin from './FxPostTradeMargin.vue'
import type { FxBorrowBuyRequest } from '@/types/fx'

const props = defineProps<{
  pairId: number | null
  currency: string
  active: boolean
  blocked: boolean
  blockedReason?: string
  accountReady: boolean
  amount: string
  borrowAmount: string
  referencePrice?: string | null
  submitting?: boolean
  pending?: boolean
}>()
const emit = defineEmits<{ submit: [body: Omit<FxBorrowBuyRequest, 'idempotency_key'>] }>()
const amount = computed(() => props.amount)
const borrowed = computed(() => props.borrowAmount)
const slippageBps = ref(50),
  now = ref(Date.now())
const valid = computed(
  () =>
    [amount.value, borrowed.value].every(
      (v) =>
        /^\d+(\.\d{0,6})?$/.test(v.trim()) &&
        compareFxAmounts(v, '0') === 1 &&
        compareFxAmounts(v, '9999999999.999999') !== 1,
    ) && compareFxAmounts(borrowed.value, amount.value) !== 1,
)
const order = computed(() =>
  props.active && props.accountReady && !props.blocked && props.pairId && valid.value
    ? {
        pairId: props.pairId,
        body: { amount: amount.value.trim(), borrow_amount: borrowed.value.trim() },
      }
    : null,
)
const { quote, loading, error, refresh, quotedReferencePrice } = useFxBorrowBuyQuote(
  order,
  () => props.referencePrice ?? null,
)
const averagePrice = computed(() =>
  quote.value ? fxGoldPerForeign({ ...quote.value, side: 'buy' }) : null,
)
const cashAmount = computed(() =>
  valid.value ? subtractFxAmounts(amount.value, borrowed.value) : null,
)
const expired = computed(() => !!quote.value && now.value >= Date.parse(quote.value.expires_at))
// A timeout only updates freshness; it never polls the account or quote API.
let expiryTimer: ReturnType<typeof setTimeout> | null = null
watch(quote, (q) => {
  if (expiryTimer) clearTimeout(expiryTimer)
  now.value = Date.now()
  if (q)
    expiryTimer = setTimeout(
      () => {
        now.value = Date.now()
      },
      Math.max(0, Date.parse(q.expires_at) - Date.now()),
    )
})
onUnmounted(() => {
  if (expiryTimer) clearTimeout(expiryTimer)
})
const minimum = computed(() =>
  quote.value?.output_amount ? computeMinOut(quote.value.output_amount, slippageBps.value) : '',
)
const canSubmit = computed(
  () =>
    props.active &&
    !props.blocked &&
    props.accountReady &&
    valid.value &&
    !loading.value &&
    quote.value?.executable &&
    !expired.value &&
    !!minimum.value,
)
const reason = computed(() =>
  quote.value?.blocked_reason
    ? mapFxError({ data: { detail: quote.value.blocked_reason } }, '本笔融资暂不能成交')
    : '',
)
const submitLabel = computed(() => {
  if (props.submitting) return '提交中…'
  if (props.pending) return '待确认订单结果'
  if (!props.accountReady) return '等待账户更新'
  if (!valid.value) return '请输入有效金额'
  if (props.blocked) return '暂不能借款买入'
  if (loading.value) return '正在更新报价…'
  if (expired.value) return '报价已过期'
  if (error.value) return '报价失败，请重试'
  if (!quote.value) return '等待报价…'
  if (!quote.value.executable) return '本笔暂不能成交'
  return `借款买入 ${fxOrderAmountLabel(amount.value)} 金圆券`
})
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
    <div class="quote-box" aria-label="借款买入报价">
      <div class="funding">
        <div>
          <span>使用现金</span><strong>{{ formatFxAmount(cashAmount, 2) }}</strong>
        </div>
        <div>
          <span>新增借款</span><strong>{{ valid ? formatFxAmount(borrowed, 2) : '—' }}</strong>
        </div>
      </div>
      <FxExecutionPrice
        :average="averagePrice"
        :reference="quotedReferencePrice"
        :currency="currency"
      />
      <dl>
        <div>
          <dt>预计买入</dt>
          <dd>{{ formatFxAmount(quote?.output_amount) }} {{ currency }}</dd>
        </div>
        <div class="quote-meta">
          <div>
            <dt>手续费（已含）</dt>
            <dd>{{ formatFxAmount(quote?.fee_amount, 2) }} 金圆券</dd>
          </div>
          <div>
            <dt>成交后风险</dt>
            <dd :class="{ danger: quote && quote.margin_status !== 'healthy' }">
              {{
                quote
                  ? {
                      healthy: '健康',
                      warning: '低于开仓门槛',
                      danger: '有强平风险',
                      blocked: '风险检查受阻',
                    }[quote.margin_status]
                  : '等待报价'
              }}
            </dd>
          </div>
        </div>
      </dl>
      <FxPostTradeMargin
        :equity="quote?.estimated_equity"
        :basis="quote?.estimated_risk_basis"
        :initial="quote?.r_initial"
        :caution="!!quote && quote.margin_status !== 'healthy'"
      />
    </div>
    <p v-if="blockedReason" class="error" role="status">{{ blockedReason }}</p>
    <p v-if="!accountReady" class="error">账户尚未更新，暂不能提交融资交易。</p>
    <p v-else-if="amount && borrowed && !valid" class="error">
      请输入有效金额，最多 6 位小数，借款不能超过买入总额。
    </p>
    <p v-if="error || reason" class="error" role="alert">{{ error || reason }}</p>
    <p v-else-if="loading" class="note" role="status">正在计算融资报价与成交后风险…</p>
    <p v-else-if="expired" class="error" role="status">报价已过期，请重新报价。</p>
    <div class="actions">
      <button class="submit" :disabled="!canSubmit" @click="submit">{{ submitLabel }}</button>
      <button class="refresh" :disabled="!order || loading || submitting" @click="refresh">
        重新报价
      </button>
    </div>
    <details>
      <summary>借款、利息与风控明细</summary>
      <p class="note">
        本金以账户清算净值为基准，本次倍数用于选规模；可否成交以报价为准。使用可用现金，差额自动借款，借款和买入一次完成。全账户共同担保；借款持续计息，保证金不足可能触发强平。
      </p>
      <dl>
        <div>
          <dt>手续费（已含）</dt>
          <dd>{{ formatFxAmount(quote?.fee_amount) }} 金圆券</dd>
        </div>
        <div>
          <dt>本次现金 / 借款</dt>
          <dd>{{ formatFxAmount(cashAmount) }} / {{ valid ? formatFxAmount(borrowed) : '—' }}</dd>
        </div>
        <div>
          <dt>成交后债务（含息）</dt>
          <dd>{{ formatFxAmount(quote?.estimated_debt) }} 金圆券</dd>
        </div>
        <div>
          <dt>系统名义杠杆上限</dt>
          <dd>{{ formatFxAmount(quote?.leverage, 2) }} 倍</dd>
        </div>
        <div>
          <dt>每日复利率</dt>
          <dd>{{ percent(quote?.daily_rate) }}</dd>
        </div>
        <div>
          <dt>最低买入数量</dt>
          <dd>{{ minimum ? formatFxAmount(minimum) : '—' }} {{ currency }}</dd>
        </div>
      </dl>
      <div v-if="quote" class="risk" :class="{ danger: quote.margin_status !== 'healthy' }">
        <strong>成交后保证金率 {{ percent(quote.equity_to_risk_basis) }}</strong>
        <span
          >开仓门槛 ≥ {{ percent(quote.r_initial) }}，强平线 &lt;
          {{ percent(quote.r_maintenance) }}</span
        >
        <span
          >清算净值 {{ formatFxAmount(quote.estimated_equity) }}，风险基数
          {{ formatFxAmount(quote.estimated_risk_basis) }}</span
        >
      </div>
      <label
        >报价变动容忍度<select v-model.number="slippageBps" :disabled="blocked">
          <option :value="50">0.5%</option>
          <option :value="100">1%</option>
          <option :value="200">2%</option>
          <option :value="500">5%</option>
        </select></label
      >
      <p class="note">
        名义杠杆是授信配置，本笔资金没有划入独立保证金账户。报价有效期 30
        秒，成交时重新检查价格与全账户风险。
      </p>
    </details>
  </div>
</template>

<style scoped>
.financing-form {
  display: flex;
  flex-direction: column;
  gap: var(--trade-panel-gap);
}
.quote-box {
  border: 1px solid #000;
  padding: 8px 10px;
  display: grid;
  gap: 6px;
  background: #fff;
}
.funding {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 0 6px;
  border-bottom: 1px solid #000;
}
.funding div {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 5px;
  flex: 1;
  min-width: 0;
}
.funding span {
  display: block;
  color: #777;
  font-size: 10px;
  margin-bottom: 0;
}
.funding strong {
  font-size: 12px;
  overflow-wrap: anywhere;
}
.funding b {
  color: #999;
  font-weight: 400;
}
label {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  font-weight: 700;
  font-size: 13px;
}
label small {
  margin-left: auto;
  color: #666;
  font-weight: 400;
}
input,
select {
  box-sizing: border-box;
  width: 100%;
  border: 2px solid #000;
  border-radius: 0;
  min-height: var(--trade-control-height);
  padding: 5px 8px;
  background: #fff;
  font: inherit;
}
input {
  font-size: 23px;
  font-variant-numeric: tabular-nums;
}
dl {
  margin: 0;
  padding: 4px 0;
  display: grid;
  gap: 7px;
}
dl div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 12px;
}
.quote-meta {
  gap: 6px 12px;
}
.quote-meta > div {
  font-size: 11px;
  gap: 5px;
}
dd {
  margin: 0;
  font-weight: 700;
  overflow-wrap: anywhere;
}
.note {
  margin: 0;
  color: #666;
  font-size: 12px;
  line-height: 1.7;
}
.error,
.danger {
  color: var(--color-down);
}
.error {
  margin: 0;
  font-size: 12px;
}
.risk {
  display: grid;
  gap: 8px;
  padding: 14px;
  border: 1px solid #ddd;
  font-size: 12px;
  margin-top: 12px;
}
.actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
button {
  border: 0;
  border-radius: 0;
  font: inherit;
  cursor: pointer;
}
.submit {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
  min-height: var(--trade-action-height);
  padding: 6px 10px;
  border: 2px solid #000;
  background: #000;
  color: #fff;
  font-weight: 700;
}
.refresh {
  flex-shrink: 0;
  padding: 8px 0;
  background: none;
  text-decoration: underline;
  font-size: 12px;
}
button:disabled,
input:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}
details {
  font-size: 12px;
}
summary {
  cursor: pointer;
  padding: 10px 0;
  font-weight: 700;
}
details label,
details p {
  margin-top: 12px;
}
@media (max-width: 640px) {
  .submit {
    min-height: 44px;
    font-size: 15px;
  }
}
@media (min-width: 951px) {
  .actions {
    position: sticky;
    bottom: 0;
    z-index: 2;
    padding-top: 8px;
    background: #fff;
  }
}
</style>
