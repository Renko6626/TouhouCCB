<script setup lang="ts">
import { computed, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { adminApi, type BatchAdjustDryRunResponse, type BatchAdjustExecuteResponse, type BatchAdjustFilter } from '@/api/admin'
import { extractErrorMessage } from '@/utils/errors'

const message = useMessage()

// 表单状态
const userIdMin = ref<string>('')
const userIdMax = ref<string>('')
const cashMin = ref<string>('')
const cashMax = ref<string>('')
const debtMin = ref<string>('')
const debtMax = ref<string>('')
const isActiveFilter = ref<'all' | 'active' | 'inactive'>('all')
const includeSuperuser = ref(false)

const amount = ref<string>('')
const reason = ref<string>('')

const dryRunResult = ref<BatchAdjustDryRunResponse | null>(null)
const executeResult = ref<BatchAdjustExecuteResponse | null>(null)
const loading = ref(false)
const error = ref('')

// 转换：空字符串 → undefined；数字字符串 → 原值（后端按 Decimal 解析）
const toOptStr = (s: string): string | undefined =>
  s.trim() === '' ? undefined : s.trim()
const toOptInt = (s: string): number | undefined => {
  const t = s.trim()
  if (t === '') return undefined
  const n = parseInt(t, 10)
  return Number.isFinite(n) ? n : undefined
}

const buildFilter = (): BatchAdjustFilter => ({
  user_id_min: toOptInt(userIdMin.value),
  user_id_max: toOptInt(userIdMax.value),
  cash_min: toOptStr(cashMin.value),
  cash_max: toOptStr(cashMax.value),
  debt_min: toOptStr(debtMin.value),
  debt_max: toOptStr(debtMax.value),
  is_active: isActiveFilter.value === 'all' ? null : isActiveFilter.value === 'active',
  include_superuser: includeSuperuser.value,
})

const amountValid = computed(() => {
  const a = Number(amount.value)
  return Number.isFinite(a) && a !== 0
})

const formValid = computed(() => {
  if (!amountValid.value) return false
  if (!reason.value.trim()) return false
  return true
})

async function runDryRun() {
  if (!formValid.value || loading.value) return
  loading.value = true
  error.value = ''
  executeResult.value = null
  try {
    const resp = await adminApi.batchAdjustCash({
      filter: buildFilter(),
      amount: amount.value,
      reason: reason.value.trim(),
      dry_run: true,
    })
    if (resp.dry_run) {
      dryRunResult.value = resp
    }
  } catch (e) {
    error.value = extractErrorMessage(e, '预览失败')
  } finally {
    loading.value = false
  }
}

async function execute() {
  if (!dryRunResult.value || loading.value) return
  if (!confirm(
    `确认对 ${dryRunResult.value.eligible_count} 个用户执行调整？\n` +
    `（将操作约 ¥${dryRunResult.value.total_delta.toFixed(2)}）`,
  )) return
  loading.value = true
  error.value = ''
  try {
    const resp = await adminApi.batchAdjustCash({
      filter: buildFilter(),
      amount: amount.value,
      reason: reason.value.trim(),
      dry_run: false,
    })
    if (!resp.dry_run) {
      executeResult.value = resp
      dryRunResult.value = null
      message.success(`已调整 ${resp.updated_count} 个用户`)
    }
  } catch (e) {
    error.value = extractErrorMessage(e, '执行失败')
  } finally {
    loading.value = false
  }
}

function resetAll() {
  userIdMin.value = ''
  userIdMax.value = ''
  cashMin.value = ''
  cashMax.value = ''
  debtMin.value = ''
  debtMax.value = ''
  isActiveFilter.value = 'all'
  includeSuperuser.value = false
  amount.value = ''
  reason.value = ''
  dryRunResult.value = null
  executeResult.value = null
  error.value = ''
}
</script>

<template>
  <div class="page">
    <header class="page-header">
      <h1 class="page-title">批量调整现金</h1>
      <p class="page-sub">按 ID / 资产范围筛选用户，批量加减现金。先预览，后执行。</p>
    </header>

    <section class="card">
      <h2 class="card-title">1 · 筛选条件</h2>

      <div class="filter-grid">
        <label class="field">
          <span class="field-label">用户 ID 起（含）</span>
          <input v-model="userIdMin" type="text" inputmode="numeric" class="input tabular-nums" placeholder="不限" />
        </label>
        <label class="field">
          <span class="field-label">用户 ID 止（含）</span>
          <input v-model="userIdMax" type="text" inputmode="numeric" class="input tabular-nums" placeholder="不限" />
        </label>

        <label class="field">
          <span class="field-label">现金 ≥</span>
          <input v-model="cashMin" type="text" inputmode="decimal" class="input tabular-nums" placeholder="不限" />
        </label>
        <label class="field">
          <span class="field-label">现金 ≤</span>
          <input v-model="cashMax" type="text" inputmode="decimal" class="input tabular-nums" placeholder="不限" />
        </label>

        <label class="field">
          <span class="field-label">债务 ≥</span>
          <input v-model="debtMin" type="text" inputmode="decimal" class="input tabular-nums" placeholder="不限" />
        </label>
        <label class="field">
          <span class="field-label">债务 ≤</span>
          <input v-model="debtMax" type="text" inputmode="decimal" class="input tabular-nums" placeholder="不限" />
        </label>

        <label class="field">
          <span class="field-label">账号状态</span>
          <select v-model="isActiveFilter" class="input">
            <option value="all">不限</option>
            <option value="active">活跃</option>
            <option value="inactive">封禁</option>
          </select>
        </label>
        <label class="field field--checkbox">
          <input type="checkbox" v-model="includeSuperuser" class="checkbox" />
          <span class="field-label">包含超管（默认排除自己）</span>
        </label>
      </div>
    </section>

    <section class="card">
      <h2 class="card-title">2 · 调整参数</h2>

      <div class="adjust-grid">
        <label class="field">
          <span class="field-label">金额（正=加，负=扣）</span>
          <input v-model="amount" type="text" inputmode="decimal" class="input tabular-nums" placeholder="例如 100 或 -50" />
        </label>
        <label class="field field--full">
          <span class="field-label">操作原因（必填，进审计日志）</span>
          <input v-model="reason" type="text" class="input" maxlength="200" placeholder="例如：开服首充奖励 / 回收测试账号余额" />
        </label>
      </div>

      <div class="action-row">
        <button class="btn-secondary" :disabled="!formValid || loading" @click="runDryRun">
          {{ loading && !dryRunResult ? '预览中…' : '预览匹配' }}
        </button>
        <button class="btn-primary" :disabled="!dryRunResult || loading" @click="execute">
          {{ loading && dryRunResult ? '执行中…' : '执行调整' }}
        </button>
        <button class="btn-link" @click="resetAll">重置</button>
      </div>

      <p v-if="error" class="error-text">
        <span class="warning-tag">错误</span>{{ error }}
      </p>
    </section>

    <!-- 预览结果 -->
    <section v-if="dryRunResult" class="card card--preview">
      <h2 class="card-title">3 · 预览匹配（dry-run）</h2>

      <div class="preview-stats">
        <div class="stat">
          <span class="stat-label">匹配总数</span>
          <span class="stat-value tabular-nums">{{ dryRunResult.matched_count }}</span>
        </div>
        <div class="stat">
          <span class="stat-label">可执行</span>
          <span class="stat-value tabular-nums">{{ dryRunResult.eligible_count }}</span>
        </div>
        <div class="stat stat--warn">
          <span class="stat-label">将跳过（操作后会为负）</span>
          <span class="stat-value tabular-nums">{{ dryRunResult.will_fail_count }}</span>
        </div>
        <div class="stat stat--accent">
          <span class="stat-label">合计调整金额</span>
          <span class="stat-value tabular-nums">¥ {{ dryRunResult.total_delta.toFixed(2) }}</span>
        </div>
      </div>

      <div v-if="dryRunResult.matched_users.length === 0" class="empty-state">没有匹配用户</div>
      <div v-else class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>用户名</th>
              <th class="text-right">现金（前）</th>
              <th class="text-right">债务</th>
              <th class="text-right">现金（后）</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="u in dryRunResult.matched_users" :key="u.id" :class="{ 'row-fail': u.will_fail }">
              <td class="tabular-nums">{{ u.id }}</td>
              <td>{{ u.username }}</td>
              <td class="text-right tabular-nums">¥ {{ u.cash_before.toFixed(2) }}</td>
              <td class="text-right tabular-nums">¥ {{ u.debt.toFixed(2) }}</td>
              <td class="text-right tabular-nums">¥ {{ u.cash_after.toFixed(2) }}</td>
              <td>
                <span v-if="u.will_fail" class="status-tag status-tag--fail">将跳过</span>
                <span v-else class="status-tag status-tag--ok">可执行</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 执行结果 -->
    <section v-if="executeResult" class="card card--result">
      <h2 class="card-title">执行完成</h2>
      <div class="preview-stats">
        <div class="stat stat--accent">
          <span class="stat-label">已调整</span>
          <span class="stat-value tabular-nums">{{ executeResult.updated_count }}</span>
        </div>
        <div v-if="executeResult.failed_count" class="stat stat--warn">
          <span class="stat-label">跳过</span>
          <span class="stat-value tabular-nums">{{ executeResult.failed_count }}</span>
        </div>
        <div class="stat">
          <span class="stat-label">实际总额</span>
          <span class="stat-value tabular-nums">¥ {{ executeResult.total_delta.toFixed(2) }}</span>
        </div>
      </div>

      <details v-if="executeResult.failed.length" class="failed-detail">
        <summary>跳过明细 ({{ executeResult.failed.length }})</summary>
        <ul class="failed-list">
          <li v-for="f in executeResult.failed" :key="f.user_id" class="failed-item">
            <span class="tabular-nums">#{{ f.user_id }}</span>
            <span class="failed-name">{{ f.username }}</span>
            <span class="failed-reason">{{ f.reason }}</span>
          </li>
        </ul>
      </details>
    </section>
  </div>
</template>

<style scoped>
.page {
  max-width: 1080px;
  margin: 0 auto;
  padding: 16px 0;
}

.page-header {
  margin-bottom: 16px;
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

.filter-grid,
.adjust-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 12px;
}
@media (min-width: 640px) {
  .filter-grid {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  }
  .adjust-grid {
    grid-template-columns: minmax(0, 1fr) minmax(0, 2fr);
  }
}

.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.field--full {
  grid-column: 1 / -1;
}
.field--checkbox {
  flex-direction: row;
  align-items: center;
  gap: 8px;
}
.field-label {
  font-size: 12px;
  font-weight: 600;
  color: #333;
}
.input,
select.input {
  padding: 7px 10px;
  border: 2px solid #000;
  background: #fff;
  font-size: 14px;
  font-family: inherit;
}
.input:focus {
  outline: none;
  background: #fafafa;
}
.checkbox {
  width: 14px;
  height: 14px;
  accent-color: #000;
}

.action-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 16px;
}
.btn-primary,
.btn-secondary {
  padding: 8px 22px;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.04em;
  border: 2px solid #000;
  cursor: pointer;
  font-family: inherit;
}
.btn-primary {
  background: #000;
  color: #fff;
  box-shadow: 4px 4px 0 #444;
}
.btn-secondary {
  background: #fff;
  color: #000;
  box-shadow: 4px 4px 0 #aaa;
}
.btn-primary:disabled,
.btn-secondary:disabled {
  background: #ddd;
  border-color: #ddd;
  color: #888;
  box-shadow: 4px 4px 0 #eee;
  cursor: not-allowed;
}
.btn-link {
  appearance: none;
  background: transparent;
  border: none;
  padding: 4px 8px;
  font-size: 13px;
  color: #555;
  cursor: pointer;
  text-decoration: underline;
}

.error-text {
  margin-top: 10px;
  font-size: 12px;
  color: #dc2626;
}

.warning-tag {
  display: inline-block;
  padding: 1px 6px;
  margin-right: 6px;
  background: #000;
  color: #fff;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.06em;
}

.preview-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px;
  margin-bottom: 16px;
}
.stat {
  border: 1.5px solid #000;
  padding: 8px 12px;
  background: #fafafa;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.stat-label {
  font-size: 11px;
  color: #666;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.stat-value {
  font-size: 18px;
  font-weight: 800;
  color: #000;
}
.stat--accent {
  background: #000;
  color: #fff;
}
.stat--accent .stat-label {
  color: #ccc;
}
.stat--accent .stat-value {
  color: #fff;
}
.stat--warn .stat-value {
  color: #dc2626;
}

.empty-state {
  padding: 32px;
  text-align: center;
  color: #999;
  font-size: 13px;
}

.table-wrap {
  overflow-x: auto;
  border: 1.5px solid #000;
}
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
  min-width: 600px;
}
.data-table th,
.data-table td {
  padding: 6px 10px;
  border-bottom: 1px solid #e0e0e0;
  text-align: left;
}
.data-table th {
  background: #000;
  color: #fff;
  font-weight: 700;
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.text-right {
  text-align: right;
}
.row-fail {
  background: #fff7f7;
  opacity: 0.85;
}

.status-tag {
  display: inline-block;
  font-size: 10px;
  font-weight: 700;
  padding: 1px 6px;
  letter-spacing: 0.04em;
  border: 1.5px solid #000;
}
.status-tag--ok {
  background: #fff;
  color: #000;
}
.status-tag--fail {
  background: #dc2626;
  color: #fff;
  border-color: #dc2626;
}

.card--preview {
  background: #fafafa;
}
.card--result {
  background: #f0fdf4;
}

.failed-detail {
  margin-top: 8px;
  border: 1px solid #ddd;
  padding: 8px 12px;
  background: #fff;
}
.failed-detail summary {
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
}
.failed-list {
  list-style: none;
  padding: 0;
  margin: 8px 0 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.failed-item {
  display: flex;
  gap: 10px;
  font-size: 12px;
  color: #555;
}
.failed-name {
  font-weight: 600;
  color: #000;
}
.failed-reason {
  color: #dc2626;
}
</style>
