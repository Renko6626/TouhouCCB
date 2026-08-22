<script setup lang="ts">
import { computed, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { adminUsersApi, type BatchAdjustDryRunResponse, type BatchAdjustExecuteResponse } from '@/api/admin'
import { extractErrorMessage } from '@/utils/errors'
import UserFilterFields from '@/components/admin/UserFilterFields.vue'
import { emptyUserFilter, toApiFilter } from '@/components/admin/userFilter'

const message = useMessage()

const filter = ref(emptyUserFilter())
const amount = ref<string>('')
const reason = ref<string>('')

const dryRunResult = ref<BatchAdjustDryRunResponse | null>(null)
const executeResult = ref<BatchAdjustExecuteResponse | null>(null)
const loading = ref(false)
const error = ref('')

const amountValid = computed(() => {
  const a = Number(amount.value)
  return Number.isFinite(a) && a !== 0
})
const formValid = computed(() => amountValid.value && !!reason.value.trim())

async function runDryRun() {
  if (!formValid.value || loading.value) return
  loading.value = true
  error.value = ''
  executeResult.value = null
  try {
    const resp = await adminUsersApi.batchAdjustCash({
      filter: toApiFilter(filter.value), amount: amount.value, reason: reason.value.trim(), dry_run: true,
    })
    if (resp.dry_run) dryRunResult.value = resp
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
    `（将操作约 金 ${dryRunResult.value.total_delta.toFixed(2)}）`,
  )) return
  loading.value = true
  error.value = ''
  try {
    const resp = await adminUsersApi.batchAdjustCash({
      filter: toApiFilter(filter.value), amount: amount.value, reason: reason.value.trim(), dry_run: false,
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
  filter.value = emptyUserFilter()
  amount.value = ''
  reason.value = ''
  dryRunResult.value = null
  executeResult.value = null
  error.value = ''
}
</script>

<template>
  <div>
    <p class="panel-intro">按 ID / 资产范围筛选用户，批量加减现金。先预览，后执行。每人写一条 <code>admin_adjust_cash</code> 流水。</p>

    <section class="card">
      <h2 class="card-title">1 · 筛选条件</h2>

      <UserFilterFields v-model="filter" />
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
          <span class="stat-value tabular-nums">金 {{ dryRunResult.total_delta.toFixed(2) }}</span>
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
              <td class="text-right tabular-nums">金 {{ u.cash_before.toFixed(2) }}</td>
              <td class="text-right tabular-nums">金 {{ u.debt.toFixed(2) }}</td>
              <td class="text-right tabular-nums">金 {{ u.cash_after.toFixed(2) }}</td>
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
          <span class="stat-value tabular-nums">金 {{ executeResult.total_delta.toFixed(2) }}</span>
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

<style scoped src="./_batch.css"></style>
