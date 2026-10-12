<script setup lang="ts">
import { computed } from 'vue'
import { compareFxAmounts, divideFxAmount, formatFxAmount, multiplyFxAmount } from '@/api/fx'
const props = defineProps<{
  kind: 'long' | 'short'
  currency: string
  quantity: string | null | undefined
  basis: string | null | undefined
  closeAmount: string | null | undefined
  pnl: string | null | undefined
  disabled: boolean
}>()
defineEmits<{ close: [] }>()
const direction = computed(() => compareFxAmounts(props.pnl, '0'))
const basisAverage = computed(() => divideFxAmount(props.basis, props.quantity, 12))
const closeAverage = computed(() => divideFxAmount(props.closeAmount, props.quantity, 12))
const percent = computed(() => {
  if (compareFxAmounts(props.basis, '0') !== 1) return null
  const ratio = divideFxAmount(props.pnl, props.basis, 12)
  return ratio == null ? null : multiplyFxAmount(ratio, '100')
})
</script>
<template>
  <article class="position-card" :class="{ up: direction === 1, down: direction === -1 }">
    <div class="heading"><strong>{{ kind === 'long' ? '多头持仓' : '空头持仓' }}</strong><small>均价单位：金圆券 / {{ currency }}</small></div>
    <dl class="metrics">
      <div><dt>{{ kind === 'long' ? '持仓数量' : '待还数量（含息）' }}</dt><dd>{{ formatFxAmount(quantity, 2) }} <small>{{ currency }}</small></dd></div>
      <div title="剩余持仓成本或开空所得除以当前数量；空头数量包含利息。"><dt>持仓等效均价</dt><dd>{{ formatFxAmount(basisAverage, 4) }}</dd></div>
      <div><dt>预计平仓均价</dt><dd>{{ formatFxAmount(closeAverage, 4) }}</dd></div>
    </dl>
    <div class="amount"><span>{{ kind === 'long' ? '预计所得' : '预计支出' }}</span><strong>{{ formatFxAmount(closeAmount, 2) }} <small>金圆券</small></strong></div>
    <div class="footer"><div class="pnl"><span>预计平仓盈亏</span><strong>{{ direction === 1 ? '+' : '' }}{{ formatFxAmount(pnl, 2) }} <small>金圆券</small><small v-if="percent != null" class="percent" :title="kind === 'long' ? '平仓盈亏 / 剩余持仓成本' : '平仓盈亏 / 剩余开空所得'">（{{ direction === 1 ? '+' : '' }}{{ formatFxAmount(percent, 2) }}%）</small></strong></div><button type="button" :disabled="disabled" @click="$emit('close')">{{ kind === 'long' ? '卖出全部' : '全部回补' }}</button></div>
  </article>
</template>
<style scoped>
.position-card{color:#111;border:1px solid #000;padding:8px 10px;background:#fafafa;--pnl-color:#555}.position-card.up{background:var(--color-up-bg);--pnl-color:var(--color-up)}.position-card.down{background:var(--color-down-bg);--pnl-color:var(--color-down)}.heading{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px}.heading strong{font-size:12px}.heading small{font-size:9px}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:0}.metrics dt,.amount>span,.pnl>span{color:#666;font-size:10px}.metrics dd{margin:2px 0 0;font-size:15px;font-weight:700;overflow-wrap:anywhere}small{font-size:10px;font-weight:400;color:#777}.amount{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-top:8px;font-size:12px}.amount>strong{font-size:15px;font-weight:700}.footer{display:flex;justify-content:space-between;align-items:center;gap:8px;border-top:1px solid #ddd;padding-top:6px;margin-top:6px}.pnl{display:grid;gap:2px;min-width:0}.pnl>strong{font-size:22px;font-weight:800;color:var(--pnl-color);overflow-wrap:anywhere}.percent{font-size:11px;color:inherit;display:inline-block}button{flex-shrink:0;border:2px solid #000;padding:6px 8px;background:#fff;font-size:11px;font-weight:700;line-height:1.2;cursor:pointer}button:disabled{opacity:.45;cursor:not-allowed}
</style>
