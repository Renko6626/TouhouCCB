<script setup lang="ts">
import { compareFxAmounts, formatFxAmount } from '@/api/fx'
import { fxOrderAmountLabel } from '@/utils/fxPresentation'

defineProps<{
  action: string
  foreign: string
  gold: string
  currency: string
  receiving: boolean
}>()

// Only shorten the button display. Quote and submission retain their exact amounts.
function brief(value: string) {
  return compareFxAmounts(value, '1') === -1
    ? fxOrderAmountLabel(value)
    : formatFxAmount(value, 0).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}
</script>

<template>
  <span class="submit-summary" :title="`${action} ${fxOrderAmountLabel(foreign)} ${currency}，预计${receiving ? '所得' : '支付'} ${fxOrderAmountLabel(gold)} 金圆券`">
    <span>{{ action }} ≈{{ brief(foreign) }} {{ currency }}</span>
    <small>{{ receiving ? '得' : '付' }} ≈金 {{ brief(gold) }}</small>
  </span>
</template>

<style scoped>
.submit-summary {
  font-size: 14px;
  font-weight: 800;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: center;
  gap: 2px 10px;
  line-height: 1.4;
}
.submit-summary > span { min-width: 0; overflow-wrap: anywhere; }
small { font-size: 12px; font-weight: 700; }
</style>
