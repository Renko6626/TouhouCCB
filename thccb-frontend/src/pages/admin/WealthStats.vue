<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { adminApi, type WealthStats } from '@/api/admin'
import { extractErrorMessage } from '@/utils/errors'

const stats = ref<WealthStats | null>(null)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    stats.value = await adminApi.wealthStats()
  } catch (e) {
    error.value = extractErrorMessage(e, '加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)

// 强平 sweep 手动触发：跟 scheduler 同逻辑，不绕过阈值
const sweepRunning = ref(false)
const sweepResult = ref('')
async function runSweep() {
  if (sweepRunning.value) return
  if (!confirm('立即跑一次强制平仓 sweep？这会对所有保证金不足的用户真实执行强平。')) return
  sweepRunning.value = true
  sweepResult.value = ''
  try {
    const r = await adminApi.runLiquidationNow()
    sweepResult.value = JSON.stringify(r)
    await load()
  } catch (e) {
    sweepResult.value = extractErrorMessage(e, '执行失败')
  } finally {
    sweepRunning.value = false
  }
}

const maxBracketCount = computed(() => {
  if (!stats.value || !stats.value.brackets.length) return 0
  return Math.max(...stats.value.brackets.map(b => b.count))
})

function fmtNum(v: number, digits = 2): string {
  return v.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function fmtBoundary(b: { lower: number | null; upper: number | null }): string {
  const lo = b.lower === null ? '∞' : b.lower.toString()
  const hi = b.upper === null ? '∞' : b.upper.toString()
  return `(${lo}, ${hi}]`
}

// 基尼系数颜色：< 0.3 健康（绿）/ 0.3-0.5 警示（灰）/ > 0.5 不平等严重（红）
const giniLevel = computed<'low' | 'mid' | 'high'>(() => {
  const g = stats.value?.gini ?? 0
  if (g < 0.3) return 'low'
  if (g < 0.5) return 'mid'
  return 'high'
})
</script>

<template>
  <div class="page">
    <header class="page-header">
      <div>
        <h1 class="page-title">平台资产统计</h1>
        <p class="page-sub">所有 active 用户的 net_worth = cash + 持仓清算价 - debt</p>
      </div>
      <div class="header-actions">
        <button class="btn-refresh" :disabled="loading" @click="load">
          {{ loading ? '加载中…' : '刷新' }}
        </button>
        <button class="btn-refresh btn-danger" :disabled="sweepRunning" @click="runSweep">
          {{ sweepRunning ? '强平中…' : '立即跑强平 sweep' }}
        </button>
      </div>
    </header>
    <p v-if="sweepResult" class="sweep-result"><span class="tag">SWEEP</span>{{ sweepResult }}</p>

    <div v-if="error" class="error-row">
      <span class="warning-tag">错误</span>{{ error }}
    </div>

    <div v-if="loading && !stats" class="loading">正在计算…</div>

    <template v-else-if="stats">
      <!-- 概览数字 -->
      <section class="card">
        <h2 class="card-title">总体</h2>
        <div class="kv-grid">
          <div class="kv">
            <span class="kv-label">用户数</span>
            <span class="kv-value tabular-nums">{{ stats.user_count }}</span>
          </div>
          <div class="kv">
            <span class="kv-label">总现金</span>
            <span class="kv-value tabular-nums">金 {{ fmtNum(stats.total_cash) }}</span>
          </div>
          <div class="kv">
            <span class="kv-label">总持仓价</span>
            <span class="kv-value tabular-nums">金 {{ fmtNum(stats.total_holdings_value) }}</span>
          </div>
          <div class="kv">
            <span class="kv-label">总债务</span>
            <span class="kv-value tabular-nums">金 {{ fmtNum(stats.total_debt) }}</span>
          </div>
          <div class="kv kv--accent">
            <span class="kv-label">总净值</span>
            <span class="kv-value tabular-nums">金 {{ fmtNum(stats.total_net_worth) }}</span>
          </div>
        </div>
      </section>

      <!-- 集中度 + 分位数 -->
      <section class="card two-col">
        <div>
          <h2 class="card-title">集中度</h2>
          <div class="kv-grid">
            <div class="kv">
              <span class="kv-label">均值</span>
              <span class="kv-value tabular-nums">金 {{ fmtNum(stats.mean) }}</span>
            </div>
            <div class="kv">
              <span class="kv-label">中位数</span>
              <span class="kv-value tabular-nums">金 {{ fmtNum(stats.median) }}</span>
            </div>
            <div class="kv">
              <span class="kv-label">标准差</span>
              <span class="kv-value tabular-nums">金 {{ fmtNum(stats.std) }}</span>
            </div>
            <div class="kv">
              <span class="kv-label">最小</span>
              <span class="kv-value tabular-nums">金 {{ fmtNum(stats.min) }}</span>
            </div>
            <div class="kv">
              <span class="kv-label">最大</span>
              <span class="kv-value tabular-nums">金 {{ fmtNum(stats.max) }}</span>
            </div>
            <div class="kv" :class="`kv-gini kv-gini--${giniLevel}`">
              <span class="kv-label">基尼系数</span>
              <span class="kv-value tabular-nums">{{ stats.gini.toFixed(4) }}</span>
              <span class="gini-hint">{{ giniLevel === 'low' ? '相对平等' : giniLevel === 'mid' ? '一般' : '高度不平等' }}</span>
            </div>
          </div>
        </div>
        <div>
          <h2 class="card-title">分位数</h2>
          <div class="kv-grid">
            <div class="kv">
              <span class="kv-label">P10</span>
              <span class="kv-value tabular-nums">金 {{ fmtNum(stats.p10) }}</span>
            </div>
            <div class="kv">
              <span class="kv-label">P25</span>
              <span class="kv-value tabular-nums">金 {{ fmtNum(stats.p25) }}</span>
            </div>
            <div class="kv">
              <span class="kv-label">P50</span>
              <span class="kv-value tabular-nums">金 {{ fmtNum(stats.p50) }}</span>
            </div>
            <div class="kv">
              <span class="kv-label">P75</span>
              <span class="kv-value tabular-nums">金 {{ fmtNum(stats.p75) }}</span>
            </div>
            <div class="kv">
              <span class="kv-label">P90</span>
              <span class="kv-value tabular-nums">金 {{ fmtNum(stats.p90) }}</span>
            </div>
            <div class="kv">
              <span class="kv-label">P99</span>
              <span class="kv-value tabular-nums">金 {{ fmtNum(stats.p99) }}</span>
            </div>
          </div>
        </div>
      </section>

      <!-- 按称号分桶分布 -->
      <section class="card">
        <h2 class="card-title">按称号分桶（净值区间）</h2>
        <div class="bracket-list">
          <div
            v-for="b in stats.brackets"
            :key="b.label"
            class="bracket-row"
          >
            <div class="bracket-meta">
              <span class="bracket-label">{{ b.label }}</span>
              <span class="bracket-range">{{ fmtBoundary(b) }}</span>
            </div>
            <div class="bracket-bar-wrap">
              <div
                class="bracket-bar"
                :style="{ width: maxBracketCount > 0 ? (b.count / maxBracketCount * 100) + '%' : '0%' }"
              ></div>
            </div>
            <div class="bracket-stats">
              <span class="bracket-count tabular-nums">{{ b.count }} 人</span>
              <span class="bracket-pct tabular-nums">{{ b.pct.toFixed(1) }}%</span>
              <span class="bracket-sum tabular-nums">金 {{ fmtNum(b.sum_wealth) }}</span>
            </div>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.page {
  max-width: 1080px;
  margin: 0 auto;
  padding: 16px 0;
}

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
  gap: 12px;
  flex-wrap: wrap;
}

.page-title {
  font-size: 22px;
  font-weight: 800;
  letter-spacing: 0.02em;
}
.page-sub {
  margin-top: 4px;
  font-size: 13px;
  color: #666;
}

.btn-refresh {
  padding: 8px 18px;
  font-size: 13px;
  font-weight: 700;
  border: 2px solid #000;
  background: #fff;
  cursor: pointer;
  box-shadow: 4px 4px 0 #aaa;
  font-family: inherit;
}
.header-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.btn-danger {
  background: #000;
  color: #fff;
  box-shadow: 4px 4px 0 #444;
}
.sweep-result {
  margin: -6px 0 14px;
  font-size: 12px;
  font-family: ui-monospace, monospace;
  word-break: break-all;
}
.sweep-result .tag {
  display: inline-block;
  padding: 1px 6px;
  margin-right: 6px;
  background: #000;
  color: #fff;
  font-weight: 800;
  font-size: 10px;
}
.btn-refresh:disabled {
  background: #eee;
  color: #888;
  cursor: not-allowed;
}

.card {
  border: 2px solid #000;
  background: #fff;
  padding: 18px 20px;
  margin-bottom: 16px;
  box-shadow: 6px 6px 0 #000;
}
.card-title {
  font-size: 13px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #000;
  margin-bottom: 14px;
  padding-bottom: 6px;
  border-bottom: 2px solid #000;
}

.two-col {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 24px;
}
@media (min-width: 800px) {
  .two-col {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  }
}

.kv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
}

.kv {
  border: 1.5px solid #000;
  padding: 8px 12px;
  background: #fafafa;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.kv-label {
  font-size: 11px;
  color: #666;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.kv-value {
  font-size: 16px;
  font-weight: 800;
  color: #000;
}
.kv--accent {
  background: #000;
  color: #fff;
}
.kv--accent .kv-label { color: #ccc; }
.kv--accent .kv-value { color: #fff; }

.kv-gini--low .kv-value { color: #16a34a; }
.kv-gini--mid .kv-value { color: #ca8a04; }
.kv-gini--high .kv-value { color: #dc2626; }
.gini-hint {
  font-size: 10px;
  color: #777;
  margin-top: 2px;
  letter-spacing: 0.04em;
}

.bracket-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.bracket-row {
  display: grid;
  grid-template-columns: minmax(140px, 200px) minmax(0, 1fr) minmax(200px, 240px);
  align-items: center;
  gap: 12px;
  padding: 8px 4px;
  border-bottom: 1px dashed #ddd;
}
.bracket-row:last-child {
  border-bottom: none;
}

.bracket-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.bracket-label {
  font-weight: 700;
  font-size: 13px;
}
.bracket-range {
  font-size: 11px;
  color: #888;
}

.bracket-bar-wrap {
  height: 18px;
  border: 1.5px solid #000;
  background: #f0f0f0;
  overflow: hidden;
}
.bracket-bar {
  height: 100%;
  background: #000;
  transition: width 0.3s;
}

.bracket-stats {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  font-size: 12px;
  color: #555;
}
.bracket-count {
  font-weight: 700;
  color: #000;
}
.bracket-pct {
  width: 50px;
  text-align: right;
}
.bracket-sum {
  color: #444;
}

.error-row {
  padding: 12px 16px;
  border: 2px solid #dc2626;
  background: #fff7f7;
  color: #dc2626;
  font-size: 13px;
  margin-bottom: 16px;
}
.warning-tag {
  display: inline-block;
  padding: 1px 6px;
  margin-right: 6px;
  background: #dc2626;
  color: #fff;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.06em;
}

.loading {
  text-align: center;
  padding: 48px;
  color: #888;
}

@media (max-width: 640px) {
  .bracket-row {
    grid-template-columns: 1fr;
    gap: 6px;
  }
  .bracket-stats {
    justify-content: flex-start;
  }
}
</style>
