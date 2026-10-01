<script setup lang="ts">
import { computed } from 'vue'
import { compareFxAmounts, formatFxAmount, subtractFxAmounts } from '@/api/fx'

const props = defineProps<{
  proceedsBasisGold: string
  referenceCoverCost: string | null
}>()

// Use the remaining proceeds basis, not restricted cash: partial covers can
// consume extra collateral without changing the remaining opening cost.
const pnl = computed(() => props.referenceCoverCost == null ? null
  : subtractFxAmounts(props.proceedsBasisGold, props.referenceCoverCost))
const direction = computed(() => compareFxAmounts(pnl.value, '0'))
const label = computed(() => direction.value === 1 ? '预计浮盈'
  : direction.value === -1 ? '预计浮亏' : '预计浮动盈亏')
</script>

<template>
  <div class="short-pnl" :class="{ profit: direction === 1, loss: direction === -1 }">
    <span class="short-pnl-label">{{ label }}</span>
    <strong class="short-pnl-amount">{{ direction === 1 ? '+' : '' }}金 {{ formatFxAmount(pnl) }}</strong>
    <p class="short-pnl-note">
      {{ pnl == null ? '回补成本暂不可用，盈亏无法估算。' : '剩余空头的未实现盈亏，按最近回补估值计算（含利息、手续费与滑点），实际以成交为准。' }}
    </p>
  </div>
</template>

<style scoped>
.short-pnl { padding: 12px 0; border-top: 1px solid #ddd; }
.short-pnl-label { display: block; font-size: 12px; font-weight: 700; color: #555; }
.short-pnl-amount { display: block; margin: 4px 0; font-size: 24px; font-weight: 800; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.profit .short-pnl-amount { color: var(--color-up, #16a34a); }
.loss .short-pnl-amount { color: var(--color-down, #dc2626); }
.short-pnl-note { margin: 0; font-size: 12px; line-height: 1.6; color: #666; }
</style>
