<script setup lang="ts">
import { ref, computed } from 'vue'
import { NButton, NInputNumber, NSelect } from 'naive-ui'
import type { SelectOption } from 'naive-ui'
import { useUserStore } from '@/stores/user'
import { useAuthStore } from '@/stores/auth'
import { useMarketStore } from '@/stores/market'
import type { MarketDetail, OutcomeQuote, QuoteResponse, Holding } from '@/types/api'

const props = defineProps<{
  market: MarketDetail | null
  selectedOutcomeId: number | null
  tradeType: 'buy' | 'sell'
  shares: number
  maxShares: number
  quoteResult: QuoteResponse | null
  estimatedNewCash: number
  userHolding: Holding | null
  quoteExceedsCash: boolean
  // 最大滑点（万分之一），undefined 时由父组件默认值兜底（默认 100=1%）
  maxSlippageBps: number
}>()

const emit = defineEmits<{
  'update:selectedOutcomeId': [value: number | null]
  'update:tradeType': [value: 'buy' | 'sell']
  'update:shares': [value: number]
  'update:maxSlippageBps': [value: number]
  'executeTrade': []
}>()

// 滑点档位（万分之一）：0.5% / 1% / 2% / 5% / 无限制
// value=-1 是 sentinel，表示"无限制"——父组件 detect 后给 API 发 accept_any_slippage=true
const SLIPPAGE_UNLIMITED = -1
const slippageOptions: SelectOption[] = [
  { label: '0.5%', value: 50 },
  { label: '1%', value: 100 },
  { label: '2%', value: 200 },
  { label: '5%', value: 500 },
  { label: '⚠ 无限制', value: SLIPPAGE_UNLIMITED },
]

const userStore = useUserStore()
const authStore = useAuthStore()
const marketStore = useMarketStore()

const outcomeOptions = computed<SelectOption[]>(() => {
  if (!props.market?.outcomes) return []
  return props.market.outcomes.map((outcome: OutcomeQuote) => ({
    label: `${outcome.label} (金 ${outcome.current_price.toFixed(2)})`,
    value: outcome.id,
  }))
})

const quickShares = computed(() => {
  const max = props.maxShares
  if (max <= 0) return [1]
  if (max <= 5) return Array.from({ length: max }, (_, i) => i + 1)
  const steps = new Set([1, Math.floor(max / 4), Math.floor(max / 2), max])
  return [...steps].filter(v => v > 0 && v <= max).sort((a, b) => a - b)
})

const isSubmitting = ref(false)

const executeTrade = async () => {
  if (!props.selectedOutcomeId || props.shares <= 0 || props.shares > props.maxShares || !props.quoteResult || props.quoteExceedsCash) return
  if (isSubmitting.value) return
  isSubmitting.value = true
  try {
    emit('executeTrade')
  } finally {
    setTimeout(() => { isSubmitting.value = false }, 500)
  }
}

// 当前选中 outcome 的边际价（用 quote 算预估滑点）
const selectedOutcomePrice = computed<number | null>(() => {
  if (!props.selectedOutcomeId || !props.market?.outcomes) return null
  const o = props.market.outcomes.find((x: OutcomeQuote) => x.id === props.selectedOutcomeId)
  return o?.current_price ?? null
})

// 预估滑点 = |avg_price - marginal_price| / marginal_price
// 反映 LMSR 大额单的真实成交滑点；用户看完这个再决定档位是否够用
const estimatedSlippagePct = computed<number | null>(() => {
  const margin = selectedOutcomePrice.value
  const avg = props.quoteResult?.avg_price
  if (!margin || !avg || margin <= 0) return null
  return Math.abs(avg - margin) / margin * 100
})

// 预估滑点是否会被当前档位拒绝（提前让用户看到红色警告）
const slippageWouldBeRejected = computed(() => {
  if (props.maxSlippageBps === SLIPPAGE_UNLIMITED) return false
  if (estimatedSlippagePct.value === null) return false
  return estimatedSlippagePct.value > (props.maxSlippageBps / 100)
})

const isUnlimitedSlippage = computed(() => props.maxSlippageBps === SLIPPAGE_UNLIMITED)

// ── 持仓与盈亏 ──
const hasHolding = computed(() =>
  !!props.userHolding && props.userHolding.amount > 0
)

const pnlDirection = computed<'up' | 'down' | 'flat'>(() => {
  const v = props.userHolding?.unrealized_pnl ?? 0
  if (v > 0) return 'up'
  if (v < 0) return 'down'
  return 'flat'
})

const pnlSign = computed(() => {
  const v = props.userHolding?.unrealized_pnl ?? 0
  return v > 0 ? '+' : v < 0 ? '−' : ''
})

const pnlPercent = computed(() => {
  const h = props.userHolding
  if (!h || h.cost_basis <= 0) return null
  return (h.unrealized_pnl / h.cost_basis) * 100
})

// 卖出均价 = market_value / amount，即"全部卖出可得的平均每份价格（含 LMSR 滑点 + 扣 sell_fee）"
// 注意：和"均价 (cost_basis/amount)"配合，amount × (卖出均价 - 均价) ≡ unrealized_pnl_liquidation
// 而 holding box 显示的"浮盈"现在是 MTM 口径（与顶部资产栏统一），所以这两个数不直接相等。
// MTM 浮盈 用 current_price（瞬时价）算；LCV 浮盈 用 market_value（含滑点）算。
const sellAvgPrice = computed(() => {
  const h = props.userHolding
  if (!h || h.amount <= 0) return null
  return h.market_value / h.amount
})

// LCV 浮盈：用于 tooltip 副显示，告诉用户"实际变现你能拿多少"
const pnlLcv = computed(() => props.userHolding?.unrealized_pnl_liquidation ?? 0)

// 整体浮盈方向（用于资产栏第 4 格着色）
const summaryPnlDirection = computed<'up' | 'down' | 'flat'>(() => {
  const v = userStore.summary?.unrealized_pnl ?? 0
  if (v > 0) return 'up'
  if (v < 0) return 'down'
  return 'flat'
})

const summaryPnlSign = computed(() => {
  const v = userStore.summary?.unrealized_pnl ?? 0
  return v > 0 ? '+' : v < 0 ? '−' : ''
})

// 执行按钮禁用原因 — 给用户具体的"为什么按不下去"
const disabledReason = computed(() => {
  if (isSubmitting.value) return '提交中…'
  if (marketStore.tradeLoading) return '处理中…'
  if (props.tradeType === 'buy' && buyDisabledByDanger.value) return '净值/借款 < 0.2，禁止买入直到补仓或被强平'
  if (!props.selectedOutcomeId) return '请先选择预测结果'
  if (props.shares <= 0) return '请输入份额'
  if (props.shares > props.maxShares) return '超过份额上限'
  if (!props.quoteResult) return '等待报价…'
  if (props.quoteExceedsCash) return '余额不足'
  return ''
})

// 一键平仓：切到 sell 并把份额填满
const closePosition = () => {
  if (!props.userHolding || props.userHolding.amount <= 0) return
  emit('update:tradeType', 'sell')
  emit('update:shares', props.userHolding.amount)
}

// danger 状态：净值/借款 < 0.2，禁止买入
const buyDisabledByDanger = computed(() => userStore.summary?.margin_status === 'danger')

// 操作类型提示
const actionHint = computed<string>(() => {
  if (!props.selectedOutcomeId || props.shares <= 0) return ''
  if (props.tradeType === 'buy') {
    return hasHolding.value ? '加仓' : '建仓'
  }
  if (!props.userHolding) return ''
  if (props.shares >= props.userHolding.amount) return '平仓'
  return '减仓'
})
</script>

<template>
  <div class="trade-panel">
    <!-- 资产概览条 -->
    <div v-if="authStore.isAuthenticated && userStore.summary" class="asset-bar">
      <div class="asset-item">
        <span class="asset-label">现金</span>
        <span class="asset-value">金 {{ userStore.summary.cash.toFixed(2) }}</span>
      </div>
      <div class="asset-item">
        <span class="asset-label">持仓</span>
        <span class="asset-value">金 {{ userStore.summary.holdings_value.toFixed(2) }}</span>
      </div>
      <div class="asset-item">
        <span class="asset-label">浮盈</span>
        <span class="asset-value" :class="`asset-pnl-${summaryPnlDirection}`">
          {{ summaryPnlSign }}金 {{ Math.abs(userStore.summary.unrealized_pnl).toFixed(2) }}
        </span>
      </div>
      <div v-if="Number(userStore.summary.debt) > 0" class="asset-item">
        <span class="asset-label">负债</span>
        <span class="asset-value asset-value--debt">金 {{ Number(userStore.summary.debt).toFixed(2) }}</span>
      </div>
      <div class="asset-item">
        <span class="asset-label">净值</span>
        <span class="asset-value asset-value--highlight">金 {{ userStore.summary.net_worth.toFixed(2) }}</span>
      </div>
    </div>

    <!-- 买/卖切换 -->
    <div class="type-switch">
      <button
        :class="['type-btn', props.tradeType === 'buy' && 'type-btn--active-buy']"
        :disabled="buyDisabledByDanger"
        :title="buyDisabledByDanger ? '净值/借款 < 0.2，禁止买入直到补仓或被强平' : undefined"
        @click="emit('update:tradeType', 'buy')"
      >买入</button>
      <button
        :class="['type-btn', props.tradeType === 'sell' && 'type-btn--active-sell']"
        :disabled="!props.userHolding"
        @click="emit('update:tradeType', 'sell')"
      >卖出</button>
    </div>

    <!-- 选项 -->
    <NSelect
      :value="props.selectedOutcomeId"
      @update:value="(v: number | null) => emit('update:selectedOutcomeId', v)"
      :options="outcomeOptions"
      placeholder="请选择预测结果"
      size="small"
    />

    <!-- 当前选项的持仓 + 浮盈（仅持有时显示） -->
    <div v-if="hasHolding && props.userHolding" class="holding-box" :class="`holding-box--${pnlDirection}`">
      <div class="holding-meta">
        <div class="holding-cell">
          <span class="holding-label">持仓</span>
          <span class="holding-value">{{ props.userHolding.amount.toLocaleString() }} 份</span>
        </div>
        <div class="holding-cell">
          <span class="holding-label">均价</span>
          <span class="holding-value">金 {{ props.userHolding.avg_price.toFixed(4) }}</span>
        </div>
        <div class="holding-cell">
          <span class="holding-label" title="全部卖出可获得的平均每份价格（已含 LMSR 滑点）">卖出均价</span>
          <span class="holding-value">金 {{ sellAvgPrice !== null ? sellAvgPrice.toFixed(4) : '—' }}</span>
        </div>
      </div>
      <div class="holding-pnl-row">
        <div
          class="holding-pnl"
          :title="`账面浮盈（按瞬时价）${pnlSign}金${Math.abs(props.userHolding.unrealized_pnl).toFixed(2)}\n立即变现浮盈（含滑点）${pnlLcv >= 0 ? '+' : '−'}金${Math.abs(pnlLcv).toFixed(2)}`"
        >
          <span class="holding-pnl-label">浮盈</span>
          <span class="holding-pnl-value" :class="`pnl-${pnlDirection}`">
            {{ pnlSign }}金 {{ Math.abs(props.userHolding.unrealized_pnl).toFixed(2) }}
            <span v-if="pnlPercent !== null" class="holding-pnl-pct">
              ({{ pnlSign }}{{ Math.abs(pnlPercent).toFixed(2) }}%)
            </span>
          </span>
        </div>
        <button
          type="button"
          class="close-position-btn"
          @click="closePosition"
          :disabled="props.userHolding.amount <= 0"
        >
          一键平仓
        </button>
      </div>
    </div>

    <!-- 份额 -->
    <div class="shares-row">
      <NInputNumber
        :value="props.shares"
        @update:value="(v: number | null) => emit('update:shares', Math.max(1, Math.floor(Number(v || 1))))"
        :min="1"
        :max="Math.max(1, props.maxShares)"
        :step="1"
        placeholder="份额"
        size="small"
        class="shares-input"
        :input-props="{ inputmode: 'numeric', pattern: '[0-9]*' }"
      />
      <NButton size="small" @click="emit('update:shares', props.maxShares)" :disabled="props.maxShares <= 0">MAX</NButton>
    </div>

    <!-- 快捷份额 -->
    <div class="quick-row">
      <NButton
        v-for="amount in quickShares"
        :key="amount"
        size="tiny"
        :type="props.shares === amount ? 'primary' : 'default'"
        @click="emit('update:shares', amount)"
        :disabled="amount > props.maxShares"
      >{{ amount }}</NButton>
      <span class="max-hint">
        最大 {{ props.maxShares }}
        <template v-if="props.tradeType === 'buy'">（预估）</template>
      </span>
    </div>

    <!-- 报价 -->
    <div v-if="props.quoteResult" class="quote-box">
      <div class="quote-row">
        <span>均价</span>
        <span>金 {{ props.quoteResult.avg_price?.toFixed(4) }}</span>
      </div>
      <div class="quote-row">
        <span>总额</span>
        <span>金 {{ props.quoteResult.gross?.toFixed(2) }}</span>
      </div>
      <div class="quote-row quote-row--dim" v-if="props.quoteResult.fee > 0">
        <span>手续费</span>
        <span>金 {{ props.quoteResult.fee?.toFixed(2) }}</span>
      </div>
      <div class="quote-row quote-row--total">
        <span>{{ props.tradeType === 'buy' ? '应付' : '到手' }}</span>
        <span>金 {{ props.quoteResult.net?.toFixed(2) }}</span>
      </div>
      <div v-if="estimatedSlippagePct !== null" class="quote-row" :class="{ 'quote-row--warn': slippageWouldBeRejected }">
        <span>预估滑点</span>
        <span>
          {{ estimatedSlippagePct.toFixed(2) }}%
          <span v-if="slippageWouldBeRejected" class="slippage-reject-hint">（超过 {{ (props.maxSlippageBps / 100).toFixed(1) }}%，会被拒）</span>
        </span>
      </div>
      <div v-if="props.quoteExceedsCash" class="quote-warn">
        余额不足，请减少份额
      </div>
    </div>

    <!-- 滑点保护：最大可接受的偏离边际价百分比；服务端 hardcap=10% -->
    <div class="slippage-row">
      <span class="slippage-label" title="若实际成交价偏离边际价超过此值，订单将被拒绝">最大滑点</span>
      <NSelect
        :value="props.maxSlippageBps"
        @update:value="(v: number) => emit('update:maxSlippageBps', v)"
        :options="slippageOptions"
        size="small"
        class="slippage-select"
      />
    </div>

    <!-- 执行按钮 -->
    <NButton
      type="primary"
      :loading="marketStore.tradeLoading"
      :disabled="isSubmitting || (props.tradeType === 'buy' && buyDisabledByDanger) || !props.selectedOutcomeId || props.shares <= 0 || props.shares > props.maxShares || !props.quoteResult || props.quoteExceedsCash"
      @click="executeTrade"
      class="exec-btn"
      :title="disabledReason"
    >
      {{ props.tradeType === 'buy' ? '买入' : '卖出' }} {{ props.shares }} 份
    </NButton>

    <!-- 操作类型提示（建仓 / 加仓 / 减仓 / 平仓） -->
    <div v-if="actionHint" class="action-hint">
      本次操作
      <span :class="['action-hint-tag', `action-hint-tag--${actionHint === '平仓' || actionHint === '减仓' ? 'sell' : 'buy'}`]">
        {{ actionHint }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.trade-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border: 2px solid #000;
  background: #fff;
}

/* 资产概览 */
.asset-bar {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 4px;
  padding: 8px 8px;
  background: #000;
  color: #fff;
  margin: -14px -14px 0 -14px;
}

.asset-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1px;
  min-width: 0;
}

.asset-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: rgba(255,255,255,0.5);
}

.asset-value {
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.asset-value--highlight {
  color: #aaffaa;
}

.asset-value--debt {
  color: #ff7a7a;
}

.asset-pnl-up    { color: var(--color-up-strong); }
.asset-pnl-down  { color: var(--color-down-strong); }
.asset-pnl-flat  { color: rgba(255, 255, 255, 0.75); }

/* 当前选项持仓块 */
.holding-box {
  border: 1.5px solid #000;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: #fafafa;
}

.holding-box--up   { background: var(--color-up-bg); border-color: #000; }
.holding-box--down { background: var(--color-down-bg); border-color: #000; }

.holding-meta {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}

.holding-cell {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.holding-label {
  font-size: 10px;
  font-weight: 600;
  color: #666;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.holding-value {
  font-size: 13px;
  font-weight: 700;
  color: #000;
  font-variant-numeric: tabular-nums;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.holding-pnl-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  border-top: 1px solid #e0e0e0;
  padding-top: 8px;
}

.holding-pnl {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.holding-pnl-label {
  font-size: 10px;
  font-weight: 600;
  color: #666;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.holding-pnl-value {
  font-size: 18px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}

.holding-pnl-pct {
  font-size: 12px;
  font-weight: 600;
  margin-left: 2px;
  opacity: 0.85;
}

.pnl-up   { color: var(--color-up); }
.pnl-down { color: var(--color-down); }
.pnl-flat { color: #555; }

/* 一键平仓按钮 */
.close-position-btn {
  flex-shrink: 0;
  padding: 8px 12px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.04em;
  border: 2px solid #000;
  background: #fff;
  color: #000;
  cursor: pointer;
  transition: transform 0.1s, box-shadow 0.1s;
}

.close-position-btn:hover:not(:disabled) {
  background: #000;
  color: #fff;
}

.close-position-btn:active:not(:disabled) {
  transform: translate(1px, 1px);
}

.close-position-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* 操作类型提示 */
.action-hint {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  font-size: 11px;
  color: #666;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.action-hint-tag {
  display: inline-block;
  padding: 2px 10px;
  font-size: 12px;
  font-weight: 700;
  border: 1.5px solid #000;
  letter-spacing: 0.04em;
}

.action-hint-tag--buy {
  background: #000;
  color: #fff;
}

.action-hint-tag--sell {
  background: var(--color-down);
  color: #fff;
  border-color: var(--color-down);
}

/* 买/卖切换 */
.type-switch {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0;
}

.type-btn {
  padding: 6px;
  font-size: 13px;
  font-weight: 700;
  border: 2px solid #000;
  background: #fff;
  color: #000;
  cursor: pointer;
}

.type-btn:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}

.type-btn--active-buy {
  background: #000;
  color: #fff;
}

.type-btn--active-sell {
  background: var(--color-down);
  color: #fff;
  border-color: var(--color-down);
}

/* 份额行 */
.shares-row {
  display: flex;
  gap: 6px;
  align-items: center;
}

.shares-input {
  flex: 1;
}

.quick-row {
  display: flex;
  gap: 4px;
  align-items: center;
  flex-wrap: wrap;
}

.max-hint {
  font-size: 11px;
  color: #888;
  margin-left: auto;
}

/* 报价 */
.quote-box {
  padding: 8px 10px;
  border: 1px solid #000;
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.quote-row {
  display: flex;
  justify-content: space-between;
}

.quote-row--dim {
  color: #888;
}

.quote-row--total {
  font-weight: 700;
  border-top: 1px solid #000;
  padding-top: 4px;
  margin-top: 2px;
}

.quote-warn {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-down);
  margin-top: 2px;
}

.quote-row--warn {
  color: var(--color-down);
  font-weight: 600;
}

.slippage-reject-hint {
  font-size: 10px;
  font-weight: 500;
  margin-left: 4px;
  color: var(--color-down);
}

/* 滑点行 */
.slippage-row {
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1.5px solid #000;
  padding: 6px 10px;
  background: #fff;
}

.slippage-label {
  cursor: help;
  text-decoration: underline dotted;
  text-underline-offset: 2px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: #000;
  flex-shrink: 0;
}

.slippage-select {
  flex: 1;
}

/* 执行按钮 */
.exec-btn {
  width: 100%;
  font-weight: 700;
}

/* 移动端：防 iOS 聚焦放大 + 更大的触控目标 */
@media (max-width: 640px) {
  .trade-panel :deep(.n-input-number .n-input__input-el),
  .trade-panel :deep(.n-base-selection-label) {
    font-size: 16px;
  }

  .type-btn {
    padding: 10px 6px;
    font-size: 14px;
  }

  .quick-row :deep(.n-button--tiny-type) {
    min-height: 32px;
    padding: 0 10px;
    font-size: 13px;
  }

  .shares-row :deep(.n-button) {
    min-height: 36px;
  }

  .exec-btn {
    min-height: 44px;
    font-size: 15px;
  }

  /* 极窄屏：一键平仓按钮换行避免挤压浮盈数字 */
  .holding-pnl-row {
    flex-wrap: wrap;
  }

  .close-position-btn {
    min-height: 36px;
    padding: 8px 14px;
  }
}
</style>
