<script setup lang="ts">
import { computed } from 'vue'

type Threshold = string | number | null
const props = withDefaults(defineProps<{
  ratio: number | null
  initial?: Threshold
  maintenance?: Threshold
  blocked?: boolean
  noRisk?: boolean
  title?: string
}>(), { initial: null, maintenance: null, blocked: false, noRisk: false, title: '账户保证金率' })

function threshold(value: Threshold) {
  if (value == null) return null
  const result = Number(value)
  return Number.isFinite(result) && result >= 0 ? result : null
}
function percent(value: number | null) {
  return value == null || !Number.isFinite(value) ? '—' : `${(value * 100).toFixed(2)}%`
}
const initialRatio = computed(() => threshold(props.initial))
const maintenanceRatio = computed(() => threshold(props.maintenance))
const status = computed(() => {
  if (props.blocked) return 'blocked'
  if (props.noRisk) return 'none'
  if (props.ratio == null || !Number.isFinite(props.ratio)
    || initialRatio.value == null || maintenanceRatio.value == null) return 'unknown'
  if (props.ratio < maintenanceRatio.value) return 'danger'
  if (props.ratio < initialRatio.value) return 'warning'
  return 'healthy'
})
const label = computed(() => ({ healthy: '健康', warning: '低于开仓门槛', danger: '低于强平线', blocked: '风险检查阻塞', none: '无风险占用', unknown: '数据待恢复' })[status.value])
</script>

<template>
  <div class="credit-risk" :class="`credit-risk-${status}`">
    <div class="credit-risk-head"><span>{{ title }}</span><span class="credit-risk-badge">{{ label }}</span></div>
    <strong class="credit-risk-ratio">{{ blocked || noRisk ? '—' : percent(ratio) }}</strong>
    <div class="credit-risk-thresholds">
      <span>开仓 / 恢复门槛 <b>≥ {{ percent(initialRatio) }}</b></span>
      <span>强平触发线 <b>&lt; {{ percent(maintenanceRatio) }}</b></span>
    </div>
    <p v-if="status === 'warning'">该保证金率不满足新增信用风险要求，可尝试还款或回补减仓。</p>
    <p v-else-if="status === 'danger'">存在定时强平风险，请优先检查回补或还款。</p>
    <p v-else-if="status === 'blocked'">估值或风险检查暂不可用；回补仍以本笔订单可执行性为准。</p>
  </div>
</template>

<style scoped>
.credit-risk { display: flex; flex-direction: column; gap: 8px; font-size: 12px; min-width: 0; }
.credit-risk-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; font-weight: 700; }
.credit-risk-badge { border: 1px solid currentColor; padding: 2px 5px; font-size: 11px; }
.credit-risk-ratio { font-size: 26px; font-variant-numeric: tabular-nums; }
.credit-risk-thresholds { display: flex; flex-direction: column; gap: 4px; color: #555; }
.credit-risk-thresholds b { color: #000; font-variant-numeric: tabular-nums; white-space: nowrap; }
.credit-risk p { margin: 0; line-height: 1.6; }
.credit-risk-warning .credit-risk-badge, .credit-risk-blocked .credit-risk-badge { color: #b45309; }
.credit-risk-danger .credit-risk-badge, .credit-risk-danger .credit-risk-ratio { color: var(--color-down, #dc2626); }
.credit-risk-healthy .credit-risk-badge { color: var(--color-up, #16a34a); }
</style>
