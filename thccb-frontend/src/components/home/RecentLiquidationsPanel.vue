<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { fetchRecentLiquidations, type LiquidationEvent } from '@/api/loan'

const events = ref<LiquidationEvent[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  const d = Math.floor(h / 24)
  return `${d} 天前`
}

function fmtNum(val: number, decimals = 2): string {
  return val.toFixed(decimals)
}

function fmtPct(val: number | null): string {
  if (val === null || val === undefined) return '—'
  return (val * 100).toFixed(1) + '%'
}

async function load() {
  loading.value = true
  error.value = null
  try {
    events.value = await fetchRecentLiquidations(10)
  } catch (e: unknown) {
    error.value = (e as { message?: string })?.message ?? '加载失败'
  } finally {
    loading.value = false
  }
}

let timer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  load()
  timer = setInterval(load, 60_000)
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="liquidations-panel">
    <div class="section-header">
      <h2 class="section-title">翻车现场</h2>
      <span class="section-sub">保证金不足触发强制平仓的公开记录 · 引以为戒</span>
    </div>

    <!-- loading -->
    <div v-if="loading && !events.length" class="liq-loading">
      <div v-for="i in 3" :key="i" class="liq-skeleton"></div>
    </div>

    <!-- error -->
    <div v-else-if="error" class="liq-error">
      <span class="liq-error-icon">!</span>
      <span>数据加载失败：{{ error }}</span>
    </div>

    <!-- empty -->
    <div v-else-if="!events.length" class="liq-empty">
      <div class="liq-empty-icon">✓</div>
      <div class="liq-empty-title">暂无翻车</div>
      <div class="liq-empty-sub">好事，目前无人触及强平线</div>
    </div>

    <!-- event list -->
    <div v-else class="liq-list">
      <div
        v-for="ev in events"
        :key="ev.id"
        class="liq-card"
        :class="{ 'liq-card-partial': !ev.fully_liquidated }"
      >
        <!-- card header -->
        <div class="liq-card-head">
          <div class="liq-user">
            <span class="liq-skull">☠</span>
            <span class="liq-username">{{ ev.username }}</span>
          </div>
          <div class="liq-meta">
            <span v-if="!ev.fully_liquidated" class="liq-tag liq-tag-partial">债务未还清</span>
            <span class="liq-tag liq-tag-source">
              {{ ev.trigger_source === 'scheduler' ? '自动触发' : '手动触发' }}
            </span>
            <span class="liq-time">{{ relativeTime(ev.triggered_at) }}</span>
          </div>
        </div>

        <!-- card body: two rows of stats -->
        <div class="liq-stats">
          <div class="liq-stat" title="按强平口径（立即变现，含 LMSR 滑点+扣手续费）记录，跟 Portfolio 顶部账面净值不直接对应">
            <span class="liq-stat-label">爆仓前净值<span class="liq-caliber-hint">·清算口径</span></span>
            <span class="liq-stat-value liq-val-red">金 {{ fmtNum(ev.pre_net_worth) }}</span>
          </div>
          <div class="liq-stat">
            <span class="liq-stat-label">爆仓前保证金率</span>
            <span class="liq-stat-value liq-val-red">{{ fmtPct(ev.pre_margin_ratio) }}</span>
          </div>
          <div class="liq-stat">
            <span class="liq-stat-label">负债</span>
            <span class="liq-stat-value">金 {{ fmtNum(ev.pre_debt) }}</span>
          </div>
          <div class="liq-stat">
            <span class="liq-stat-label">平仓仓位数</span>
            <span class="liq-stat-value">{{ ev.sold_positions_count }}</span>
          </div>
          <div class="liq-stat">
            <span class="liq-stat-label">变现所得</span>
            <span class="liq-stat-value">金 {{ fmtNum(ev.total_proceeds) }}</span>
          </div>
          <div class="liq-stat">
            <span class="liq-stat-label">已偿债务</span>
            <span class="liq-stat-value">金 {{ fmtNum(ev.repaid_amount) }}</span>
          </div>
          <div v-if="!ev.fully_liquidated" class="liq-stat liq-stat-full">
            <span class="liq-stat-label">剩余债务</span>
            <span class="liq-stat-value liq-val-danger">金 {{ fmtNum(ev.remaining_debt) }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.liquidations-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* header */
.section-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  border-bottom: 2px solid #000000;
  padding-bottom: 10px;
  flex-wrap: wrap;
  gap: 6px;
}

.section-title {
  font-size: 18px;
  font-weight: 700;
  color: #000000;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.section-sub {
  font-size: 11px;
  color: #888888;
  letter-spacing: 0.02em;
}

/* loading skeleton */
.liq-loading {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.liq-skeleton {
  height: 110px;
  border: 2px solid #e0e0e0;
  background: linear-gradient(90deg, #f5f5f5 25%, #ebebeb 50%, #f5f5f5 75%);
  background-size: 200% 100%;
  animation: skeleton-shimmer 1.4s infinite;
}

@keyframes skeleton-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* error */
.liq-error {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 16px;
  border: 2px solid #cc0000;
  background: #fff5f5;
  font-size: 13px;
  color: #cc0000;
  font-weight: 600;
}

.liq-error-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border: 2px solid #cc0000;
  font-weight: 900;
  font-size: 12px;
  flex-shrink: 0;
}

/* empty */
.liq-empty {
  padding: 40px 24px;
  text-align: center;
  border: 2px solid #cccccc;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}

.liq-empty-icon {
  font-size: 28px;
  font-weight: 900;
  color: #22aa44;
  line-height: 1;
}

.liq-empty-title {
  font-size: 14px;
  font-weight: 700;
  color: #000000;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.liq-empty-sub {
  font-size: 12px;
  color: #888888;
}

/* list */
.liq-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* card */
.liq-card {
  border: 2px solid #cc0000;
  background: #ffffff;
  padding: 14px 16px;
  position: relative;
}

.liq-card::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 4px;
  background: #cc0000;
}

.liq-card-partial {
  border-color: #880000;
}

.liq-card-partial::before {
  background: #880000;
}

/* card header */
.liq-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
  padding-left: 8px;
}

.liq-user {
  display: flex;
  align-items: center;
  gap: 6px;
}

.liq-skull {
  font-size: 16px;
  line-height: 1;
  color: #cc0000;
}

.liq-username {
  font-size: 14px;
  font-weight: 800;
  color: #000000;
  letter-spacing: 0.02em;
}

.liq-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.liq-tag {
  font-size: 10px;
  font-weight: 700;
  padding: 2px 6px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  border: 1.5px solid currentColor;
}

.liq-tag-partial {
  color: #880000;
  border-color: #880000;
  background: #fff0f0;
}

.liq-tag-source {
  color: #666666;
  border-color: #666666;
}

.liq-time {
  font-size: 11px;
  color: #888888;
  font-variant-numeric: tabular-nums;
}

/* stats grid */
.liq-stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0;
  border: 1px solid #dddddd;
  margin-left: 8px;
}

.liq-stat {
  padding: 8px 12px;
  display: flex;
  flex-direction: column;
  gap: 3px;
  border-right: 1px solid #dddddd;
  border-bottom: 1px solid #dddddd;
}

.liq-stat:nth-child(3n) {
  border-right: none;
}

/* last row: remove bottom border from the last 3 cells */
.liq-stat:nth-last-child(-n+3) {
  border-bottom: none;
}

/* if odd item spans full row */
.liq-stat-full {
  grid-column: 1 / -1;
  border-right: none;
}

.liq-stat-label {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #888888;
}

.liq-caliber-hint {
  font-size: 9px;
  font-weight: 500;
  color: #aaaaaa;
  margin-left: 4px;
  text-transform: none;
  letter-spacing: 0;
}

.liq-stat-value {
  font-size: 14px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  color: #000000;
}

.liq-val-red {
  color: #cc0000;
}

.liq-val-danger {
  color: #880000;
  font-size: 15px;
}

/* responsive */
@media (max-width: 768px) {
  .liq-stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .liq-stat:nth-child(3n) {
    border-right: 1px solid #dddddd;
  }

  .liq-stat:nth-child(2n) {
    border-right: none;
  }

  /* reset last-row rule, let it be handled naturally */
  .liq-stat:nth-last-child(-n+3) {
    border-bottom: 1px solid #dddddd;
  }

  .liq-stat:nth-last-child(-n+2) {
    border-bottom: none;
  }

  /* if total count is odd, last lone item */
  .liq-stat:last-child {
    border-bottom: none;
  }

  .section-header {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
