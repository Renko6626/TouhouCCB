<script setup lang="ts">
/**
 * 用户管理 — 管理员对单个用户的所有操作的唯一入口：
 * 资产（调现金）/ 贷款（强制放贷、免债）/ 账号（封禁、管理员）/ 称号。
 * 批量操作见 BatchOps.vue。
 */
import { computed, h, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  useMessage, useDialog, NDataTable, NButton, NInput, NTabs, NTabPane, NSelect, NTag,
  type DataTableColumns,
} from 'naive-ui'
import {
  adminUsersApi, adminTitleApi,
  type UserListItem, type AdminUserSummary, type UserTitleItem,
} from '@/api/admin'
import type { TitleRead } from '@/api/title'
import { useAuthStore } from '@/stores/auth'
import { extractErrorMessage } from '@/utils/errors'
import TitleChip from '@/components/title/TitleChip.vue'

const message = useMessage()
const dialog = useDialog()
const authStore = useAuthStore()
const route = useRoute()
const router = useRouter()

// ── 列表 ──
const users = ref<UserListItem[]>([])
const allTitles = ref<TitleRead[]>([])
const listLoading = ref(false)
const search = ref('')

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return users.value
  return users.value.filter(u => String(u.id) === q || u.username.toLowerCase().includes(q))
})

async function loadList() {
  listLoading.value = true
  try {
    const [u, t] = await Promise.all([adminUsersApi.list(), adminTitleApi.listTitles()])
    users.value = u
    allTitles.value = t
  } catch (e) {
    message.error(extractErrorMessage(e, '加载失败'))
  } finally {
    listLoading.value = false
  }
}

// ── 详情 ──
const selectedId = ref<number | null>(null)
const summary = ref<AdminUserSummary | null>(null)
const userTitles = ref<UserTitleItem[]>([])
const detailLoading = ref(false)
const isSelf = computed(() => selectedId.value !== null && selectedId.value === authStore.user?.id)

async function loadDetail() {
  if (selectedId.value === null) return
  detailLoading.value = true
  try {
    const [s, t] = await Promise.all([
      adminUsersApi.get(selectedId.value),
      adminTitleApi.listUserTitles(selectedId.value),
    ])
    summary.value = s
    userTitles.value = t
  } catch (e) {
    message.error(extractErrorMessage(e, '加载用户详情失败'))
  } finally {
    detailLoading.value = false
  }
}

function select(id: number) {
  selectedId.value = id
  summary.value = null
  userTitles.value = []
  router.replace({ query: { ...route.query, uid: String(id) } })
  loadDetail()
}

/** 任一操作成功后：刷新详情 + 列表里的那一行 */
async function afterMutation() {
  await Promise.all([loadDetail(), loadList()])
}

function confirmDialog(title: string, content: string, positiveText = '确认'): Promise<boolean> {
  return new Promise(resolve => {
    dialog.warning({
      title, content, positiveText, negativeText: '取消',
      onPositiveClick: () => resolve(true),
      onNegativeClick: () => resolve(false),
      onClose: () => resolve(false),
    })
  })
}

// ── 资产：调现金 ──
const cashAmount = ref('')
const cashReason = ref('')
const cashBusy = ref(false)
const cashValid = computed(() => {
  const n = Number(cashAmount.value)
  return Number.isFinite(n) && n !== 0 && !!cashReason.value.trim()
})

async function doAdjustCash() {
  if (!summary.value || !cashValid.value) return
  const n = Number(cashAmount.value)
  const verb = n > 0 ? '加' : '扣'
  if (!await confirmDialog(`${verb}钱`, `给 ${summary.value.username}（#${summary.value.user_id}）${verb} 金 ${Math.abs(n).toFixed(2)}？`)) return
  cashBusy.value = true
  try {
    const r = await adminUsersApi.adjustCash(summary.value.user_id, cashAmount.value.trim(), cashReason.value.trim())
    message.success(`已${verb}钱，当前现金 金 ${r.new_cash.toFixed(2)}`)
    cashAmount.value = ''
    cashReason.value = ''
    await afterMutation()
  } catch (e) {
    message.error(extractErrorMessage(e, '操作失败'))
  } finally {
    cashBusy.value = false
  }
}

// ── 贷款：强制放贷 / 免债 ──
const loanAmount = ref('')
const loanReason = ref('')
const loanBusy = ref(false)
const loanValid = computed(() => Number(loanAmount.value) > 0 && !!loanReason.value.trim())

const forgiveAmount = ref('')
const forgiveReason = ref('')
const forgiveBusy = ref(false)
const forgiveValid = computed(() => Number(forgiveAmount.value) > 0 && !!forgiveReason.value.trim())

async function doForceLoan() {
  if (!summary.value || !loanValid.value) return
  if (!await confirmDialog('强制放贷', `给 ${summary.value.username} 放贷 金 ${Number(loanAmount.value).toFixed(2)}？现金与债务同时增加，按当前利率计息。`)) return
  loanBusy.value = true
  try {
    const r = await adminUsersApi.forceLoan(summary.value.user_id, loanAmount.value.trim(), loanReason.value.trim())
    message.success(`放贷成功，债务 金 ${r.debt.toFixed(2)}`)
    loanAmount.value = ''
    loanReason.value = ''
    await afterMutation()
  } catch (e) {
    message.error(extractErrorMessage(e, '放贷失败'))
  } finally {
    loanBusy.value = false
  }
}

function fillFullDebt() {
  if (summary.value) forgiveAmount.value = summary.value.debt.toFixed(2)
}

async function doForgiveDebt() {
  if (!summary.value || !forgiveValid.value) return
  if (!await confirmDialog('免除债务', `免除 ${summary.value.username} 债务 金 ${Number(forgiveAmount.value).toFixed(2)}？超过结息后债务的部分自动截断。`)) return
  forgiveBusy.value = true
  try {
    const r = await adminUsersApi.forgiveDebt(summary.value.user_id, forgiveAmount.value.trim(), forgiveReason.value.trim())
    message.success(`已免除 金 ${(r.effective ?? 0).toFixed(2)}，剩余债务 金 ${r.debt.toFixed(2)}`)
    forgiveAmount.value = ''
    forgiveReason.value = ''
    await afterMutation()
  } catch (e) {
    message.error(extractErrorMessage(e, '免债失败'))
  } finally {
    forgiveBusy.value = false
  }
}

// ── 账号：封禁 / 管理员 ──
const banReason = ref('')
const accountBusy = ref(false)

async function doBan() {
  if (!summary.value) return
  if (!await confirmDialog('封禁', `封禁 ${summary.value.username}？封后无法访问任何业务接口，可随时解封。`, '封禁')) return
  accountBusy.value = true
  try {
    await adminUsersApi.ban(summary.value.user_id, banReason.value.trim() || undefined)
    message.success('已封禁')
    banReason.value = ''
    await afterMutation()
  } catch (e) {
    message.error(extractErrorMessage(e, '封禁失败'))
  } finally {
    accountBusy.value = false
  }
}

async function doUnban() {
  if (!summary.value) return
  accountBusy.value = true
  try {
    await adminUsersApi.unban(summary.value.user_id)
    message.success('已解封')
    await afterMutation()
  } catch (e) {
    message.error(extractErrorMessage(e, '解封失败'))
  } finally {
    accountBusy.value = false
  }
}

async function toggleAdmin() {
  if (!summary.value || isSelf.value) return
  const target = !summary.value.is_superuser
  const verb = target ? '提升为管理员' : '取消管理员'
  if (!await confirmDialog(verb, `${verb}：${summary.value.username}（#${summary.value.user_id}）？`)) return
  accountBusy.value = true
  try {
    const r = await adminUsersApi.setRole(summary.value.user_id, target)
    message.success(r.changed ? `已${verb}` : '状态未变更')
    await afterMutation()
  } catch (e) {
    message.error(extractErrorMessage(e, '操作失败'))
  } finally {
    accountBusy.value = false
  }
}

// ── 称号 ──
const grantTitleId = ref<number | null>(null)
const titleBusy = ref(false)
const titleOptions = computed(() =>
  allTitles.value
    .filter(t => t.is_active && !userTitles.value.some(ut => ut.title.id === t.id))
    .map(t => ({ label: t.name, value: t.id })),
)

async function doGrant() {
  if (!summary.value || !grantTitleId.value) return
  titleBusy.value = true
  try {
    await adminTitleApi.grantTitle(summary.value.user_id, grantTitleId.value)
    grantTitleId.value = null
    message.success('已授予')
    await loadDetail()
  } catch (e) {
    message.error(extractErrorMessage(e, '授予失败'))
  } finally {
    titleBusy.value = false
  }
}

async function doRevoke(tid: number) {
  if (!summary.value) return
  if (!await confirmDialog('撤销称号', '确认撤销该称号？', '撤销')) return
  try {
    await adminTitleApi.revokeTitle(summary.value.user_id, tid)
    message.success('已撤销')
    await loadDetail()
  } catch (e) {
    message.error(extractErrorMessage(e, '撤销失败'))
  }
}

// ── 表格 ──
const columns: DataTableColumns<UserListItem> = [
  { title: 'ID', key: 'id', width: 52 },
  { title: '用户名', key: 'username', minWidth: 120, ellipsis: { tooltip: true } },
  { title: '现金', key: 'cash', width: 84, align: 'right', render: r => r.cash.toFixed(2) },
  { title: '债务', key: 'debt', width: 84, align: 'right', render: r => r.debt.toFixed(2) },
  {
    title: '', key: '_status', width: 88,
    render(r) {
      return h('div', { class: 'flex gap-1' }, [
        r.is_active ? null : h(NTag, { type: 'error', size: 'small' }, () => '封禁'),
        r.is_superuser ? h(NTag, { type: 'warning', size: 'small' }, () => '管理员') : null,
      ])
    },
  },
]
const rowProps = (row: UserListItem) => ({
  style: 'cursor:pointer',
  class: row.id === selectedId.value ? 'row-selected' : '',
  onClick: () => select(row.id),
})

onMounted(async () => {
  await loadList()
  const uid = Number(route.query.uid)
  if (Number.isInteger(uid) && uid > 0) select(uid)
})
</script>

<template>
  <div class="page">
    <header class="page-header">
      <div>
        <h1 class="page-title">用户管理</h1>
        <p class="page-sub">点一行查看并操作该用户：资产 / 贷款 / 账号 / 称号。批量操作请去「批量操作」页。</p>
      </div>
      <RouterLink to="/admin/users/batch" class="btn-outline">批量操作 →</RouterLink>
    </header>

    <div class="split">
      <section class="card list-card">
        <NInput v-model:value="search" placeholder="搜索用户名 / 精确 ID" clearable size="small" class="mb-2" />
        <NDataTable
          :columns="columns" :data="filtered" :loading="listLoading"
          :bordered="true" size="small" :max-height="560"
          :row-key="(r: UserListItem) => r.id" :row-props="rowProps"
        />
        <p class="list-foot">共 {{ users.length }} 人（最多显示 200）</p>
      </section>

      <section class="card detail-card">
        <div v-if="selectedId === null" class="empty-state">← 在左侧选择一个用户</div>
        <div v-else-if="!summary" class="empty-state">{{ detailLoading ? '加载中…' : '加载失败' }}</div>
        <template v-else>
          <div class="detail-head">
            <div>
              <div class="detail-name">
                {{ summary.username }}
                <span class="detail-id">#{{ summary.user_id }}</span>
                <NTag v-if="!summary.is_active" type="error" size="small">封禁</NTag>
                <NTag v-if="summary.is_superuser" type="warning" size="small">管理员</NTag>
                <NTag v-if="isSelf" size="small">这是你</NTag>
              </div>
              <div class="detail-email">{{ summary.email }}</div>
            </div>
            <TitleChip v-if="summary.equipped_title" :title="summary.equipped_title" />
          </div>

          <div class="kv">
            <div class="kv-item">
              <span class="kv-label">现金</span>
              <span class="kv-value tabular-nums">金 {{ summary.cash.toFixed(2) }}</span>
            </div>
            <div class="kv-item" :class="{ 'kv-item--warn': summary.debt > 0 }">
              <span class="kv-label">债务</span>
              <span class="kv-value tabular-nums">金 {{ summary.debt.toFixed(2) }}</span>
            </div>
            <div class="kv-item">
              <span class="kv-label">上次结息</span>
              <span class="kv-value">{{ summary.debt_last_accrued_at ? new Date(summary.debt_last_accrued_at).toLocaleString('zh-CN') : '—' }}</span>
            </div>
          </div>

          <NTabs type="line" default-value="cash" size="small">
            <NTabPane name="cash" tab="资产">
              <div class="form">
                <label class="field">
                  <span class="field-label">金额（正=加，负=扣）</span>
                  <input v-model="cashAmount" type="text" inputmode="decimal" class="input tabular-nums" placeholder="例如 100 或 -50" />
                </label>
                <label class="field">
                  <span class="field-label">原因（必填，进流水）</span>
                  <input v-model="cashReason" type="text" class="input" maxlength="200" placeholder="例如：活动补偿" />
                </label>
                <button class="btn-primary" :disabled="!cashValid || cashBusy" @click="doAdjustCash">
                  {{ cashBusy ? '提交中…' : '调整现金' }}
                </button>
              </div>
            </NTabPane>

            <NTabPane name="loan" tab="贷款">
              <div class="form">
                <h3 class="form-title">强制放贷</h3>
                <p class="form-hint">受站点配置 loan_enabled 约束；现金与债务同时增加。</p>
                <label class="field">
                  <span class="field-label">金额（&gt;0）</span>
                  <input v-model="loanAmount" type="text" inputmode="decimal" class="input tabular-nums" placeholder="例如 500" />
                </label>
                <label class="field">
                  <span class="field-label">原因（必填）</span>
                  <input v-model="loanReason" type="text" class="input" maxlength="200" />
                </label>
                <button class="btn-primary" :disabled="!loanValid || loanBusy" @click="doForceLoan">
                  {{ loanBusy ? '提交中…' : '放贷' }}
                </button>

                <h3 class="form-title mt">免除债务</h3>
                <p class="form-hint">先按当前利率结息再扣减；超过债务的部分自动截断。</p>
                <label class="field">
                  <span class="field-label">金额（&gt;0）</span>
                  <div class="inline">
                    <input v-model="forgiveAmount" type="text" inputmode="decimal" class="input tabular-nums" placeholder="例如 100" />
                    <button class="btn-outline btn-sm" :disabled="summary.debt <= 0" @click="fillFullDebt">全额</button>
                  </div>
                </label>
                <label class="field">
                  <span class="field-label">原因（必填）</span>
                  <input v-model="forgiveReason" type="text" class="input" maxlength="200" />
                </label>
                <button class="btn-primary" :disabled="!forgiveValid || forgiveBusy || summary.debt <= 0" @click="doForgiveDebt">
                  {{ forgiveBusy ? '提交中…' : '免债' }}
                </button>
              </div>
            </NTabPane>

            <NTabPane name="account" tab="账号">
              <div class="form">
                <h3 class="form-title">封禁</h3>
                <template v-if="summary.is_active">
                  <label class="field">
                    <span class="field-label">原因（选填，写入日志）</span>
                    <input v-model="banReason" type="text" class="input" maxlength="500" />
                  </label>
                  <button class="btn-danger" :disabled="accountBusy || isSelf" @click="doBan">封禁此用户</button>
                  <p v-if="isSelf" class="form-hint">不能封禁自己。</p>
                </template>
                <template v-else>
                  <p class="form-hint">该用户当前处于封禁状态。</p>
                  <button class="btn-primary" :disabled="accountBusy" @click="doUnban">解封</button>
                </template>

                <h3 class="form-title mt">管理员权限</h3>
                <p class="form-hint">不能修改自己；不能取消最后一个管理员。</p>
                <button class="btn-outline" :disabled="accountBusy || isSelf" @click="toggleAdmin">
                  {{ summary.is_superuser ? '取消管理员' : '提升为管理员' }}
                </button>
              </div>
            </NTabPane>

            <NTabPane name="title" tab="称号">
              <div class="form">
                <div v-if="userTitles.length === 0" class="form-hint">暂无称号</div>
                <div v-for="ut in userTitles" :key="ut.title.id" class="title-row">
                  <div class="inline">
                    <TitleChip :title="ut.title" />
                    <span class="form-hint">来源: {{ ut.source }}</span>
                  </div>
                  <button class="btn-outline btn-sm" @click="doRevoke(ut.title.id)">撤销</button>
                </div>
                <div class="inline mt">
                  <NSelect v-model:value="grantTitleId" :options="titleOptions" placeholder="选称号授予" clearable size="small" style="flex:1" />
                  <NButton size="small" type="primary" :loading="titleBusy" :disabled="!grantTitleId" @click="doGrant">授予</NButton>
                </div>
              </div>
            </NTabPane>
          </NTabs>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.page {
  max-width: 1200px;
  margin: 0 auto;
  padding: 16px 0;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 12px;
  flex-wrap: wrap;
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
.split {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
@media (min-width: 960px) {
  .split {
    grid-template-columns: minmax(0, 5fr) minmax(0, 6fr);
  }
}
.card {
  border: 2px solid #000;
  background: #fff;
  padding: 16px 18px;
  box-shadow: 6px 6px 0 #000;
}
.list-foot {
  margin-top: 8px;
  font-size: 11px;
  color: #888;
}
:deep(.row-selected td) {
  background: #000 !important;
  color: #fff !important;
}
.empty-state {
  padding: 48px 16px;
  text-align: center;
  color: #999;
  font-size: 13px;
}
.detail-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  padding-bottom: 10px;
  border-bottom: 2px solid #000;
  margin-bottom: 12px;
}
.detail-name {
  font-size: 18px;
  font-weight: 800;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.detail-id {
  font-size: 13px;
  font-weight: 600;
  color: #888;
}
.detail-email {
  font-size: 12px;
  color: #666;
}
.kv {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 8px;
  margin-bottom: 14px;
}
.kv-item {
  border: 1.5px solid #000;
  background: #fafafa;
  padding: 6px 10px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.kv-item--warn {
  background: #000;
  color: #fff;
}
.kv-item--warn .kv-label {
  color: #ccc;
}
.kv-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #666;
}
.kv-value {
  font-size: 15px;
  font-weight: 800;
}
.form {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-top: 6px;
}
.form-title {
  font-size: 12px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  border-bottom: 2px solid #000;
  padding-bottom: 4px;
}
.form-hint {
  font-size: 12px;
  color: #666;
  margin: 0;
}
.mt {
  margin-top: 12px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.field-label {
  font-size: 12px;
  font-weight: 600;
  color: #333;
}
.inline {
  display: flex;
  gap: 8px;
  align-items: center;
}
.inline .input {
  flex: 1;
}
.input {
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
.btn-primary,
.btn-outline,
.btn-danger {
  align-self: flex-start;
  padding: 8px 20px;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.04em;
  border: 2px solid #000;
  cursor: pointer;
  font-family: inherit;
  text-decoration: none;
}
.btn-primary {
  background: #000;
  color: #fff;
  box-shadow: 4px 4px 0 #444;
}
.btn-outline {
  background: #fff;
  color: #000;
  box-shadow: 4px 4px 0 #aaa;
}
.btn-danger {
  background: #b91c1c;
  border-color: #b91c1c;
  color: #fff;
  box-shadow: 4px 4px 0 #444;
}
.btn-sm {
  padding: 5px 12px;
  font-size: 12px;
  box-shadow: none;
}
.btn-primary:disabled,
.btn-outline:disabled,
.btn-danger:disabled {
  background: #ddd;
  border-color: #ddd;
  color: #888;
  box-shadow: none;
  cursor: not-allowed;
}
.title-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border: 1.5px solid #000;
  padding: 6px 10px;
}
</style>
