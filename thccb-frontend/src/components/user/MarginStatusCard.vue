<script setup lang="ts">
import { computed } from 'vue'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()
const summary = computed(() => userStore.summary)

const shouldShow = computed(() => {
  const s = summary.value
  return s != null && Number(s.debt) > 0
})

const ratio = computed(() => summary.value?.margin_ratio ?? null)
const status = computed(() => summary.value?.margin_status ?? 'healthy')
const hardThr = computed(() => summary.value?.margin_hard_threshold ?? 0.2)
const softThr = computed(() => summary.value?.margin_soft_threshold ?? 0.5)
// net_worth 是 MTM 主显示（账面），net_worth_liquidation 是 LCV（保证金计算用）
const netWorth = computed(() => Number(summary.value?.net_worth ?? 0))
const netWorthLcv = computed(() => Number(summary.value?.net_worth_liquidation ?? 0))
const debt = computed(() => Number(summary.value?.debt ?? 0))
// 两口径差距（LMSR 滑点 + 手续费的损耗）
const slippageGap = computed(() => netWorth.value - netWorthLcv.value)

// 距离强平线的相对缓冲：净值再跌 X% 触发
//   触发条件：NW' = hard × debt
//   当前:    NW  = ratio × debt
//   缓冲 = 1 - hard/ratio  (ratio > hard 时为正)
const bufferPct = computed(() => {
  if (ratio.value == null || ratio.value <= 0) return null
  if (ratio.value <= hardThr.value) return 0
  return (1 - hardThr.value / ratio.value) * 100
})

const statusLabel = computed(() => {
  if (status.value === 'danger') return '危险'
  if (status.value === 'warning') return '警戒'
  return '健康'
})

const lastLiquidatedAt = computed(() => summary.value?.last_liquidated_at)
const sinceLastLiquidated = computed(() => {
  if (!lastLiquidatedAt.value) return null
  return Date.now() - new Date(lastLiquidatedAt.value).getTime()
})
const showJustLiquidated = computed(() => {
  const ms = sinceLastLiquidated.value
  return ms !== null && ms >= 0 && ms < 24 * 3600 * 1000
})

function relativeTime(ms: number): string {
  const m = Math.floor(ms / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  return `${Math.floor(h / 24)} 天前`
}
</script>

<template>
  <div
    v-if="shouldShow"
    class="margin-status-card"
    :class="`status-${status}`"
  >
    <div class="card-header">
      <h3 class="card-title">保证金率</h3>
      <span class="card-badge" :class="`badge-${status}`">{{ statusLabel }}</span>
    </div>

    <div class="ratio-row">
      <span class="ratio-big">
        {{ ratio != null ? Number(ratio).toFixed(3) : '—' }}
      </span>
      <div class="ratio-meta">
        <div class="formula">
          立即变现 <span class="num">{{ netWorthLcv.toFixed(2) }}</span>
          ÷ 借款 <span class="num">{{ debt.toFixed(2) }}</span>
        </div>
        <div v-if="slippageGap > 0.01" class="formula-explain">
          账面净值 <span class="num">{{ netWorth.toFixed(2) }}</span>，
          滑点损耗 <span class="num">-{{ slippageGap.toFixed(2) }}</span>
          <span class="hint">（保证金按保守"立即变现"口径算）</span>
        </div>
        <div class="thresholds">
          强平线 <span class="num">{{ Number(hardThr).toFixed(2) }}</span>
          · 警戒线 <span class="num">{{ Number(softThr).toFixed(2) }}</span>
        </div>
      </div>
    </div>

    <div v-if="bufferPct !== null" class="buffer-row">
      <div class="buffer-head">
        <span class="buffer-label">距强平缓冲</span>
        <span class="buffer-pct">{{ bufferPct.toFixed(1) }}%</span>
      </div>
      <div class="buffer-bar">
        <div
          class="buffer-fill"
          :class="`fill-${status}`"
          :style="{ width: `${Math.min(100, Math.max(0, bufferPct))}%` }"
        ></div>
      </div>
      <div class="buffer-hint">
        净值再跌 <span class="num">{{ bufferPct.toFixed(1) }}%</span> 触发强制平仓
        <span class="caliber-hint">（按清算口径，比顶部账面更保守）</span>
      </div>
    </div>

    <div v-if="showJustLiquidated" class="just-liquidated-chip">
      <span class="chip-icon">☠</span>
      <span>上次被强平：{{ relativeTime(sinceLastLiquidated!) }}</span>
    </div>
  </div>
</template>

<style scoped>
.margin-status-card {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px;
  border: 2px solid #000000;
  background: #ffffff;
  position: relative;
}

.margin-status-card.status-warning {
  border-color: #d97706;
}

.margin-status-card.status-danger {
  border-color: #dc2626;
  background: #fff8f8;
}

.card-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  border-bottom: 2px solid currentColor;
  padding-bottom: 8px;
}

.card-title {
  font-size: 14px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #000000;
  margin: 0;
}

.card-badge {
  font-size: 11px;
  font-weight: 800;
  padding: 3px 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  border: 1.5px solid currentColor;
}

.badge-healthy {
  color: #166534;
  border-color: #166534;
  background: #ecfdf5;
}

.badge-warning {
  color: #92400e;
  border-color: #d97706;
  background: #fef3c7;
}

.badge-danger {
  color: #991b1b;
  border-color: #dc2626;
  background: #fee2e2;
}

.ratio-row {
  display: flex;
  align-items: center;
  gap: 18px;
}

.ratio-big {
  font-size: 34px;
  font-weight: 900;
  font-variant-numeric: tabular-nums;
  color: #000000;
  line-height: 1;
  letter-spacing: -0.01em;
}

.status-warning .ratio-big {
  color: #92400e;
}

.status-danger .ratio-big {
  color: #991b1b;
}

.ratio-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: #444444;
}

.formula,
.thresholds {
  font-weight: 600;
  letter-spacing: 0.02em;
}

.formula-explain {
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.02em;
  color: #666666;
}

.hint {
  color: #888888;
  font-weight: 500;
}

.num {
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  color: #000000;
}

.buffer-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.buffer-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}

.buffer-label {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #888888;
}

.buffer-pct {
  font-size: 13px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  color: #000000;
}

.buffer-bar {
  height: 10px;
  background: #f0f0f0;
  border: 1.5px solid #000000;
  position: relative;
  overflow: hidden;
}

.buffer-fill {
  height: 100%;
  transition: width 0.4s ease-out;
}

.fill-healthy {
  background: #16a34a;
}

.fill-warning {
  background: #d97706;
}

.fill-danger {
  background: #dc2626;
}

.buffer-hint {
  font-size: 11px;
  color: #666666;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.caliber-hint {
  font-size: 10px;
  font-weight: 500;
  color: #999999;
}

.just-liquidated-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  align-self: flex-start;
  padding: 5px 10px;
  border: 2px solid #4b5563;
  background: #f3f4f6;
  color: #1f2937;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
}

.chip-icon {
  font-size: 13px;
  color: #4b5563;
  line-height: 1;
}
</style>
