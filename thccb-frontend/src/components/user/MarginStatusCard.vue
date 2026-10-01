<script setup lang="ts">
import { computed } from 'vue'
import { useUserStore } from '@/stores/user'
import { compareFxAmounts } from '@/api/fx'
import CreditRiskStatus from './CreditRiskStatus.vue'

const userStore = useUserStore()
const summary = computed(() => userStore.summary)

const shouldShow = computed(() => {
  const s = summary.value
  return s != null && (Number(s.debt) > 0 || !!s.short_positions?.length || s.risk_status === 'blocked')
})

const ratio = computed(() => userStore.marginRatioEstimate)
const baseStatus = computed(() => summary.value?.unified_credit_enabled ? summary.value.risk_status ?? 'blocked' : summary.value?.margin_status ?? 'healthy')
const protectedByHalt = computed(() => !summary.value?.unified_credit_enabled && (summary.value?.liquidation_protected ?? false))
// HALT 保护优先：danger/warning + HALT 持仓 → 显示保护态而非危险
const status = computed(() =>
  protectedByHalt.value && baseStatus.value !== 'healthy' ? 'protected' : baseStatus.value
)
const hardThr = computed(() => summary.value?.unified_credit_enabled
  ? summary.value.r_maintenance ?? null : summary.value?.margin_hard_threshold ?? 0.2)
const softThr = computed(() => summary.value?.unified_credit_enabled
  ? summary.value.r_initial ?? null : summary.value?.margin_soft_threshold ?? 0.5)
// net_worth 是 MTM 主显示（账面），net_worth_liquidation 是 LCV（保证金计算用）
const netWorth = computed(() => userStore.netWorth)
const netWorthLcv = computed(() => userStore.netWorthLcv)
const debt = computed(() => Number(summary.value?.debt_with_interest ?? summary.value?.debt ?? 0))
// 两口径差距（LMSR 滑点 + 手续费的损耗）
const slippageGap = computed(() => netWorth.value == null || netWorthLcv.value == null ? null : netWorth.value - netWorthLcv.value)

const statusLabel = computed(() => {
  if (status.value === 'blocked') return '风险检查阻塞'
  if (status.value === 'protected') return '熔断保护'
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

    <p v-if="summary?.unified_credit_enabled">清算净值与风险状态为最近刷新快照；定时扫描按最新资产执行。</p>
    <p v-if="summary?.unified_credit_enabled">
      总现金 C：金 {{ summary.cash.toFixed(2) }} · 未锁定现金 C−S：金 {{ summary.available_cash ?? '—' }}
      <br>金圆券借款 D（含息）：金 {{ debt.toFixed(2) }} · 外币全仓回补成本 K：金 {{ summary.short_cover_cost ?? '—' }}
      <br>外币义务按币种列示于持仓明细；B 是风险基数，保证金率为 E/B。
    </p>
    <p v-if="status === 'blocked'">{{ summary?.blocked_reason || '估值待恢复，新增风险暂不可用。' }}</p>
    <CreditRiskStatus
      :ratio="ratio" :initial="softThr" :maintenance="hardThr"
      :blocked="status === 'blocked'"
      :protected="status === 'protected'"
      :legacy="summary?.unified_credit_enabled === false"
      :no-risk="summary?.unified_credit_enabled === true && compareFxAmounts(summary.risk_basis, '0') === 0"
    />
    <details class="risk-details">
      <summary>估值口径与风险说明</summary>
      <p>清算净值 金 {{ netWorthLcv?.toFixed(2) ?? '—' }}；账面净值 金 {{ netWorth?.toFixed(2) ?? '—' }}。
        <span v-if="slippageGap != null && slippageGap > 0.01">估值差 金 {{ slippageGap.toFixed(2) }}。</span>
      </p>
      <p>强平按最新的清算净值与风险基数判断。外币回补成本会随行情变化，保证金率距离强平线的差值不等于固定的汇率或净值跌幅。</p>
    </details>

    <div v-if="showJustLiquidated" class="just-liquidated-chip">
      <span class="chip-icon">☠</span>
      <span>上次被强平：{{ relativeTime(sinceLastLiquidated!) }}</span>
    </div>
  </div>
</template>

<style scoped>
.risk-details { font-size: 12px; line-height: 1.6; }
.risk-details summary { cursor: pointer; font-weight: 700; }
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

.margin-status-card.status-protected {
  border-color: #2563eb;
  background: #f5f9ff;
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

.badge-blocked { color: #92400e; background: #fef3c7; }

.badge-protected {
  color: #1e3a8a;
  border-color: #2563eb;
  background: #dbeafe;
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
