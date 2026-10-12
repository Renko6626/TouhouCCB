<script setup lang="ts">
import { computed } from 'vue'
import { formatFxPrice, tradeSlippageBps } from '@/api/fx'

const props = defineProps<{
  average: string | null
  reference: string | null
  currency: string
}>()
const deviation = computed(() => tradeSlippageBps(props.reference, props.average))
// Only a visual warning; quote-to-submit slippage protection stays independent.
const prominent = computed(() => deviation.value != null && deviation.value >= 100)
</script>

<template>
  <div class="execution-price">
    <div class="price-row">
      <span>成交均价（含费）</span>
      <strong
        >{{ formatFxPrice(average) }} <small>金圆券 / {{ currency }}</small></strong
      >
    </div>
    <div
      class="deviation-row"
      :class="{ prominent }"
      :title="`成交均价与参考汇率 ${formatFxPrice(reference)} 金圆券 / ${currency}的差异，已含手续费。`"
    >
      <span>偏离参考价</span>
      <strong>{{ deviation == null ? '—' : `${(deviation / 100).toFixed(2)}%` }}</strong>
    </div>
  </div>
</template>

<style scoped>
.execution-price {
  display: grid;
  gap: 5px;
  padding: 0 0 6px;
}
.price-row,
.deviation-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.price-row {
  font-size: 12px;
}
.deviation-row {
  font-size: 11px;
  color: #777;
}
.price-row > span {
  color: #777;
}
strong {
  text-align: right;
  overflow-wrap: anywhere;
  min-width: 0;
}
small {
  font-size: 10px;
  font-weight: 400;
}
.prominent {
  background: #fff1ef;
  color: var(--color-down);
  font-weight: 700;
  padding: 3px 5px;
  border-left: 2px solid var(--color-down);
}
</style>
