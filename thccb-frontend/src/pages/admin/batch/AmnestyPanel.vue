<script setup lang="ts">
import { computed, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { adminUsersApi, type AmnestyDryRunResponse, type AmnestyExecuteResponse } from '@/api/admin'
import { extractErrorMessage } from '@/utils/errors'
import UserFilterFields from '@/components/admin/UserFilterFields.vue'
import { emptyUserFilter, toApiFilter } from '@/components/admin/userFilter'

const CONFIRM_PHRASE = '大赦天下'

const message = useMessage()

const filter = ref(emptyUserFilter())
// 空串 → 后端取 site_config.initial_balance
const resetCashTo = ref<string>('')
const forgiveDebt = ref(true)
const reason = ref<string>('')
const confirmText = ref('')

const dryRunResult = ref<AmnestyDryRunResponse | null>(null)
const executeResult = ref<AmnestyExecuteResponse | null>(null)
const loading = ref(false)
const error = ref('')

const resetValid = computed(() => {
  const t = resetCashTo.value.trim()
  if (t === '') return true
  const n = Number(t)
  return Number.isFinite(n) && n >= 0
})
const formValid = computed(() => resetValid.value && !!reason.value.trim())
const confirmOk = computed(() => confirmText.value.trim() === CONFIRM_PHRASE)

function payload(dryRun: boolean) {
  const t = resetCashTo.value.trim()
  return {
    filter: toApiFilter(filter.value),
    reset_cash_to: t === '' ? null : t,
    forgive_debt: forgiveDebt.value,
    reason: reason.value.trim(),
    dry_run: dryRun,
  }
}

function fmtDelta(v: number): string {
  return `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(2)}`
}

async function runDryRun() {
  if (!formValid.value || loading.value) return
  loading.value = true
  error.value = ''
  executeResult.value = null
  confirmText.value = ''
  try {
    const resp = await adminUsersApi.amnesty(payload(true))
    if (resp.dry_run) dryRunResult.value = resp
  } catch (e) {
    error.value = extractErrorMessage(e, '预览失败')
  } finally {
    loading.value = false
  }
}

async function execute() {
  if (!dryRunResult.value || !confirmOk.value || loading.value) return
  loading.value = true
  error.value = ''
  try {
    const resp = await adminUsersApi.amnesty(payload(false))
    if (!resp.dry_run) {
      executeResult.value = resp
      dryRunResult.value = null
      confirmText.value = ''
      message.success(`大赦完成：${resp.updated_count} 人`)
    }
  } catch (e) {
    error.value = extractErrorMessage(e, '执行失败')
  } finally {
    loading.value = false
  }
}

function resetAll() {
  filter.value = emptyUserFilter()
  resetCashTo.value = ''
  forgiveDebt.value = true
  reason.value = ''
  confirmText.value = ''
  dryRunResult.value = null
  executeResult.value = null
  error.value = ''
}
</script>

<template>
  <div>
    <p class="panel-intro">
      对匹配用户<strong>清零债务（先结息）并把现金还原到目标值</strong>——现金高于目标的同样被降下来，这是「还原」不是「补足」。
      <strong>持仓不动</strong>，市场状态不受影响。每人写一条 <code>admin_amnesty</code> 流水；
      此操作会在资产统计 / 排行榜上留下一个不可逆的断层。
    </p>

    <section class="card">
      <h2 class="card-title">1 · 筛选条件</h2>
      <UserFilterFields v-model="filter" />
    </section>

    <section class="card">
      <h2 class="card-title">2 · 大赦参数</h2>

      <div class="adjust-grid">
        <label class="field">
          <span class="field-label">现金还原到</span>
          <input
            v-model="resetCashTo" type="text" inputmode="decimal"
            class="input tabular-nums" :class="{ 'input--invalid': !resetValid }"
            placeholder="留空 = 站点配置 initial_balance"
          />
        </label>
        <label class="field field--full">
          <span class="field-label">操作原因（必填，进审计日志）</span>
          <input v-model="reason" type="text" class="input" maxlength="200" placeholder="例如：S2 赛季重开 / 经济崩盘救济" />
        </label>
        <label class="field field--checkbox field--full">
          <input v-model="forgiveDebt" type="checkbox" class="checkbox" />
          <span class="field-label">同时免除全部债务（先按当前利率结息再清零）</span>
        </label>
      </div>

      <div class="action-row">
        <button class="btn-secondary" :disabled="!formValid || loading" @click="runDryRun">
          {{ loading && !dryRunResult ? '预览中…' : '预览匹配' }}
        </button>
        <button class="btn-link" @click="resetAll">重置</button>
      </div>

      <p v-if="error" class="error-text">
        <span class="warning-tag">错误</span>{{ error }}
      </p>
    </section>

    <!-- 预览 -->
    <section v-if="dryRunResult" class="card card--preview">
      <h2 class="card-title">3 · 预览（dry-run）</h2>

      <div class="preview-stats">
        <div class="stat">
          <span class="stat-label">匹配人数</span>
          <span class="stat-value tabular-nums">{{ dryRunResult.matched_count }}</span>
        </div>
        <div class="stat">
          <span class="stat-label">现金还原到</span>
          <span class="stat-value tabular-nums">金 {{ dryRunResult.reset_cash_to.toFixed(2) }}</span>
        </div>
        <div class="stat stat--accent">
          <span class="stat-label">现金净注入</span>
          <span class="stat-value tabular-nums">金 {{ fmtDelta(dryRunResult.total_cash_delta) }}</span>
        </div>
        <div class="stat" :class="dryRunResult.forgive_debt && dryRunResult.total_debt_forgiven > 0 ? 'stat--warn' : ''">
          <span class="stat-label">免除债务（未结息估算）</span>
          <span class="stat-value tabular-nums">
            {{ dryRunResult.forgive_debt ? `金 ${dryRunResult.total_debt_forgiven.toFixed(2)}` : '不免债' }}
          </span>
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
              <th class="text-right">现金（后）</th>
              <th class="text-right">债务（前）</th>
              <th class="text-right">债务（后）</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="u in dryRunResult.matched_users" :key="u.id">
              <td class="tabular-nums">{{ u.id }}</td>
              <td>{{ u.username }}</td>
              <td class="text-right tabular-nums">金 {{ u.cash_before.toFixed(2) }}</td>
              <td class="text-right tabular-nums" :class="u.cash_after < u.cash_before ? 'num-down' : u.cash_after > u.cash_before ? 'num-up' : ''">
                金 {{ u.cash_after.toFixed(2) }}
              </td>
              <td class="text-right tabular-nums">金 {{ u.debt_before.toFixed(2) }}</td>
              <td class="text-right tabular-nums">金 {{ u.debt_after.toFixed(2) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="dryRunResult.matched_count > 0" class="confirm-box">
        <div class="confirm-title">确认执行</div>
        <p class="confirm-hint">
          这会真实改写 <strong>{{ dryRunResult.matched_count }}</strong> 个账户的现金与债务，不可撤销。
          输入「<strong>{{ CONFIRM_PHRASE }}</strong>」解锁执行按钮。
        </p>
        <div class="confirm-row">
          <input v-model="confirmText" type="text" class="input" :placeholder="CONFIRM_PHRASE" />
          <button class="btn-primary" :disabled="!confirmOk || loading" @click="execute">
            {{ loading ? '执行中…' : '执行大赦' }}
          </button>
        </div>
      </div>
    </section>

    <!-- 结果 -->
    <section v-if="executeResult" class="card card--result">
      <h2 class="card-title">大赦完成</h2>
      <div class="preview-stats">
        <div class="stat stat--accent">
          <span class="stat-label">已重置</span>
          <span class="stat-value tabular-nums">{{ executeResult.updated_count }} 人</span>
        </div>
        <div class="stat">
          <span class="stat-label">现金净注入</span>
          <span class="stat-value tabular-nums">金 {{ fmtDelta(executeResult.total_cash_delta) }}</span>
        </div>
        <div class="stat">
          <span class="stat-label">实际免除债务（含利息）</span>
          <span class="stat-value tabular-nums">金 {{ executeResult.total_debt_forgiven.toFixed(2) }}</span>
        </div>
      </div>
      <details class="failed-detail">
        <summary>逐人明细 ({{ executeResult.updated.length }})</summary>
        <div class="table-wrap" style="margin-top: 8px">
          <table class="data-table">
            <thead>
              <tr>
                <th>ID</th><th>用户名</th>
                <th class="text-right">现金 前 → 后</th>
                <th class="text-right">债务 前 → 后</th>
                <th class="text-right">免除</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="u in executeResult.updated" :key="u.user_id">
                <td class="tabular-nums">{{ u.user_id }}</td>
                <td>{{ u.username }}</td>
                <td class="text-right tabular-nums">{{ u.cash_before.toFixed(2) }} → {{ u.cash_after.toFixed(2) }}</td>
                <td class="text-right tabular-nums">{{ u.debt_before.toFixed(2) }} → {{ u.debt_after.toFixed(2) }}</td>
                <td class="text-right tabular-nums">{{ u.debt_forgiven.toFixed(2) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>
    </section>
  </div>
</template>

<style scoped src="./_batch.css"></style>
<style scoped>
.input--invalid {
  border-color: #dc2626;
}
.num-up {
  color: #16a34a;
}
.num-down {
  color: #dc2626;
}
.confirm-box {
  margin-top: 16px;
  border: 3px solid #000;
  background: #fff;
  padding: 14px 16px;
  box-shadow: 6px 6px 0 #000;
}
.confirm-title {
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin-bottom: 6px;
}
.confirm-hint {
  font-size: 13px;
  color: #333;
  margin: 0 0 10px;
  line-height: 1.6;
}
.confirm-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.confirm-row .input {
  flex: 1;
  min-width: 180px;
  max-width: 320px;
}
</style>
