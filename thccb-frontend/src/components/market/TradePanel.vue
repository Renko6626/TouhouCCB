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
}>()

const emit = defineEmits<{
  'update:selectedOutcomeId': [value: number | null]
  'update:tradeType': [value: 'buy' | 'sell']
  'update:shares': [value: number]
  'executeTrade': []
}>()

const userStore = useUserStore()
const authStore = useAuthStore()
const marketStore = useMarketStore()

const outcomeOptions = computed<SelectOption[]>(() => {
  if (!props.market?.outcomes) return []
  return props.market.outcomes.map((outcome: OutcomeQuote) => ({
    label: `${outcome.label} (¥${outcome.current_price.toFixed(2)})`,
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
</script>

<template>
  <div class="trade-panel">
    <!-- 资产概览条 -->
    <div v-if="authStore.isAuthenticated && userStore.summary" class="asset-bar">
      <div class="asset-item">
        <span class="asset-label">现金</span>
        <span class="asset-value">¥{{ userStore.summary.cash.toFixed(2) }}</span>
      </div>
      <div class="asset-item">
        <span class="asset-label">持仓</span>
        <span class="asset-value">¥{{ userStore.summary.holdings_value.toFixed(2) }}</span>
      </div>
      <div class="asset-item">
        <span class="asset-label">净值</span>
        <span class="asset-value asset-value--highlight">¥{{ userStore.summary.net_worth.toFixed(2) }}</span>
      </div>
    </div>

    <!-- 买/卖切换 -->
    <div class="type-switch">
      <button
        :class="['type-btn', props.tradeType === 'buy' && 'type-btn--active-buy']"
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
      placeholder="选择选项"
      size="small"
    />

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
        <template v-if="props.tradeType === 'buy'"> (估)</template>
      </span>
    </div>

    <!-- 报价 -->
    <div v-if="props.quoteResult" class="quote-box">
      <div class="quote-row">
        <span>均价</span>
        <span>¥{{ props.quoteResult.avg_price?.toFixed(4) }}</span>
      </div>
      <div class="quote-row">
        <span>总额</span>
        <span>¥{{ props.quoteResult.gross?.toFixed(2) }}</span>
      </div>
      <div class="quote-row quote-row--dim" v-if="props.quoteResult.fee > 0">
        <span>手续费</span>
        <span>¥{{ props.quoteResult.fee?.toFixed(2) }}</span>
      </div>
      <div class="quote-row quote-row--total">
        <span>{{ props.tradeType === 'buy' ? '应付' : '到手' }}</span>
        <span>¥{{ props.quoteResult.net?.toFixed(2) }}</span>
      </div>
      <div v-if="props.quoteExceedsCash" class="quote-warn">
        余额不足，请减少份额
      </div>
    </div>

    <!-- 执行按钮 -->
    <NButton
      type="primary"
      :loading="marketStore.tradeLoading"
      :disabled="isSubmitting || !props.selectedOutcomeId || props.shares <= 0 || props.shares > props.maxShares || !props.quoteResult || props.quoteExceedsCash"
      @click="executeTrade"
      class="exec-btn"
    >
      {{ props.tradeType === 'buy' ? '买入' : '卖出' }} {{ props.shares }} 份
    </NButton>

    <!-- 当前持仓（卖出模式时显示） -->
    <div v-if="props.userHolding && props.tradeType === 'sell'" class="holding-hint">
      持仓 {{ props.userHolding.amount }} 份
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
  display: flex;
  justify-content: space-between;
  padding: 8px 10px;
  background: #000;
  color: #fff;
  margin: -14px -14px 0 -14px;
}

.asset-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1px;
}

.asset-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: rgba(255,255,255,0.5);
}

.asset-value {
  font-size: 13px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.asset-value--highlight {
  color: #aaffaa;
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
  background: #dc2626;
  color: #fff;
  border-color: #dc2626;
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
  color: #c00;
  margin-top: 2px;
}

/* 执行按钮 */
.exec-btn {
  width: 100%;
  font-weight: 700;
}

.holding-hint {
  font-size: 11px;
  color: #666;
  text-align: center;
}
</style>
