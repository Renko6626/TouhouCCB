<script setup lang="ts">
import { computed } from 'vue'
import { NCard } from 'naive-ui'
import type { OutcomeQuote } from '@/types/api'

const props = defineProps<{
  outcome: OutcomeQuote
  isSelected: boolean
  onClick?: () => void
}>()

const probabilityPercent = computed(() => {
  return (props.outcome.current_price * 100).toFixed(1)
})

const heatLabel = computed(() => {
  const p = props.outcome.current_price
  if (p > 0.7) return '热门'
  if (p < 0.3) return '冷门'
  return '中性'
})
</script>

<template>
  <NCard
    :title="props.outcome.label"
    hoverable
    :class="['outcome-card', { 'outcome-card--selected': props.isSelected }]"
    @click="props.onClick"
  >
    <div class="outcome-rows">
      <div class="outcome-row">
        <span class="outcome-label">当前价格:</span>
        <span class="outcome-value outcome-value--bold">
          ¥{{ props.outcome.current_price.toFixed(4) }}
          <span
            v-if="props.outcome.price_change_pct_24h != null"
            :class="['change-tag', props.outcome.price_change_pct_24h > 0 ? 'up' : props.outcome.price_change_pct_24h < 0 ? 'down' : '']"
          >
            {{ props.outcome.price_change_pct_24h > 0 ? '+' : '' }}{{ props.outcome.price_change_pct_24h.toFixed(1) }}%
          </span>
        </span>
      </div>

      <div class="outcome-row">
        <span class="outcome-label">总份额:</span>
        <span class="outcome-value">{{ props.outcome.total_shares.toLocaleString() }}</span>
      </div>

      <div v-if="props.outcome.payout != null" class="outcome-row">
        <span class="outcome-label">赔付:</span>
        <span class="outcome-value">¥{{ props.outcome.payout.toFixed(2) }}</span>
      </div>

      <div class="outcome-row">
        <span class="outcome-label">隐含概率:</span>
        <span class="probability-tag">{{ probabilityPercent }}%</span>
      </div>
    </div>

    <template #footer>
      <div class="outcome-footer">
        <span class="outcome-label">{{ props.outcome.total_shares.toLocaleString() }} 份</span>
        <span class="heat-tag">{{ heatLabel }}</span>
      </div>
    </template>
  </NCard>
</template>

<style scoped>
.outcome-card--selected {
  border-color: #000 !important;
  box-shadow: 6px 6px 0 #000 !important;
}

.outcome-rows {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.outcome-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.outcome-label {
  font-size: 13px;
  color: #555;
}

.outcome-value {
  font-size: 14px;
}

.outcome-value--bold {
  font-size: 16px;
  font-weight: 700;
}

.change-tag {
  font-size: 11px;
  font-weight: 700;
  margin-left: 4px;
  font-variant-numeric: tabular-nums;
  color: #888;
}

.change-tag.up {
  color: var(--color-up);
}

.change-tag.down {
  color: var(--color-down);
}

.probability-tag,
.heat-tag {
  display: inline-block;
  padding: 1px 8px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  border: 1.5px solid #000;
  background: #000;
  color: #fff;
}

.outcome-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
}
</style>
