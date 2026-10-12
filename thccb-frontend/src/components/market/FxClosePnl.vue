<script setup lang="ts">
import { computed } from 'vue'
import { compareFxAmounts, formatFxAmount } from '@/api/fx'

const props = defineProps<{
  pnl: string | null
  note: string
  unavailable: string
}>()
const direction = computed(() => compareFxAmounts(props.pnl, '0'))
</script>

<template>
  <div class="close-pnl" :class="{ profit: direction === 1, loss: direction === -1 }">
    <div class="pnl-row">
      <span>预计平仓盈亏</span>
      <strong
        >{{ direction === 1 ? '+' : '' }}{{ formatFxAmount(pnl, 2) }} <small>金圆券</small></strong
      >
    </div>
    <p v-if="pnl == null">{{ unavailable }}</p>
    <details v-else class="pnl-note">
      <summary>盈亏说明</summary>
      <p>{{ note }}</p>
    </details>
  </div>
</template>

<style scoped>
.close-pnl {
  border-top: 1px solid #ddd;
  padding-top: 8px;
  margin-top: 5px;
}
.pnl-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.pnl-row > span {
  font-size: 12px;
  font-weight: 700;
}
strong {
  font-size: 19px;
  font-weight: 700;
  overflow-wrap: anywhere;
  text-align: right;
  min-width: 0;
  font-variant-numeric: tabular-nums;
}
small {
  font-size: 10px;
  font-weight: 400;
}
.profit strong {
  color: var(--color-up);
}
.loss strong {
  color: var(--color-down);
}
summary {
  color: #777;
  font-size: 10px;
  cursor: pointer;
}
.pnl-note {
  margin-top: 4px;
}
p {
  margin: 4px 0 0;
  color: #777;
  font-size: 10px;
  line-height: 1.5;
}
</style>
