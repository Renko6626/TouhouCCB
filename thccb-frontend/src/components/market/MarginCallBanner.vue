<script setup lang="ts">
import { computed } from 'vue'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()

const status = computed(() => userStore.summary?.margin_status ?? 'healthy')
const ratio = computed(() => userStore.marginRatioEstimate)
const hardThr = computed(() => userStore.summary?.margin_hard_threshold ?? 0.2)
const softThr = computed(() => userStore.summary?.margin_soft_threshold ?? 0.5)
const lastLiquidatedAt = computed(() => userStore.summary?.last_liquidated_at)
// HALT 市场有持仓 → sweep 守卫保护中。优先显示保护态而非 danger/warning。
const protectedByHalt = computed(() => userStore.summary?.liquidation_protected ?? false)

// 距上次强平的毫秒数，null 表示从未强平或未取到
const sinceLastLiquidated = computed(() => {
  if (!lastLiquidatedAt.value) return null
  return Date.now() - new Date(lastLiquidatedAt.value).getTime()
})

// 24h 内刚被强平 → 即使现在 status=healthy 也要给个 chip
const showJustLiquidatedChip = computed(() => {
  const ms = sinceLastLiquidated.value
  return ms !== null && ms >= 0 && ms < 24 * 3600 * 1000
})

const visible = computed(
  () => status.value !== 'healthy' || showJustLiquidatedChip.value || protectedByHalt.value
)

function formatRatio(r: number | null | undefined): string {
  return r != null ? r.toFixed(3) : '?'
}

function relativeTime(ms: number): string {
  const m = Math.floor(ms / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  return `${Math.floor(h / 24)} 天前`
}

const message = computed(() => {
  if (status.value === 'danger') {
    return `即将被强平 — 净值/借款 = ${formatRatio(ratio.value)} < ${hardThr.value.toFixed(2)}，请立即补仓或卖出持仓`
  }
  if (status.value === 'warning') {
    return `重仓警报 — 净值/借款 = ${formatRatio(ratio.value)} < ${softThr.value.toFixed(2)}，建议补仓或减仓避免被强平`
  }
  return ''
})

const justLiquidatedMessage = computed(() => {
  const ms = sinceLastLiquidated.value
  if (ms == null) return ''
  return `你刚于 ${relativeTime(ms)} 被强制平仓`
})

// banner 主体仅在 warning/danger 时显示；healthy + 刚被强平时只显示底部 chip。
// HALT 保护态优先：即使 status=danger，protectedByHalt=true 时显示 info 蓝色而非 danger 红色，
// 避免"红色告警但实际不会强平"的体感困惑（review I3）。
const showMainBanner = computed(
  () => status.value !== 'healthy' || protectedByHalt.value
)

// 显示模式：'protected' > 'danger' > 'warning'（HALT 保护优先）
const displayMode = computed<'protected' | 'danger' | 'warning' | null>(() => {
  if (protectedByHalt.value && status.value !== 'healthy') return 'protected'
  if (status.value === 'danger') return 'danger'
  if (status.value === 'warning') return 'warning'
  return null
})

const protectedMessage = computed(() => {
  const r = formatRatio(ratio.value)
  return `市场熔断保护中 — 净值/借款 = ${r}（按清算口径），但你持有 HALT 市场持仓，sweep 跳过本轮强平。待市场恢复后再判定。`
})
</script>

<template>
  <div v-if="visible" class="margin-call-wrap">
    <div
      v-if="showMainBanner"
      class="margin-call-banner"
      :class="`status-${displayMode}`"
    >
      <span class="banner-label">
        {{ displayMode === 'protected' ? 'HALT PROTECTED'
           : displayMode === 'danger' ? 'MARGIN CALL'
           : 'MARGIN WARNING' }}
      </span>
      <span class="banner-message">
        {{ displayMode === 'protected' ? protectedMessage : message }}
      </span>
    </div>

    <div v-if="showJustLiquidatedChip" class="just-liquidated-chip">
      <span class="chip-icon">☠</span>
      <span class="chip-text">{{ justLiquidatedMessage }}</span>
    </div>
  </div>
</template>

<style scoped>
.margin-call-wrap {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
}

.margin-call-banner {
  display: flex;
  align-items: baseline;
  gap: 12px;
  padding: 10px 16px;
  border-width: 2px;
  border-style: solid;
  border-radius: 0;
  letter-spacing: 0.03em;
}

.banner-label {
  font-size: 11px;
  font-weight: 900;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  white-space: nowrap;
  flex-shrink: 0;
}

.banner-message {
  font-size: 13px;
  font-weight: 600;
  line-height: 1.4;
}

.status-warning {
  background: #fef3c7;
  color: #92400e;
  border-color: #d97706;
}

.status-danger {
  background: #fee2e2;
  color: #991b1b;
  border-color: #dc2626;
  font-weight: 700;
}

.status-protected {
  background: #dbeafe;
  color: #1e3a8a;
  border-color: #2563eb;
}

.just-liquidated-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  align-self: flex-start;
  padding: 6px 12px;
  border: 2px solid #4b5563;
  background: #f3f4f6;
  color: #1f2937;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.04em;
}

.chip-icon {
  font-size: 14px;
  color: #4b5563;
  line-height: 1;
}

.chip-text {
  font-variant-numeric: tabular-nums;
}
</style>
