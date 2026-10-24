<script setup lang="ts">
/**
 * 用户管理 — 管理员对单个用户的所有操作的唯一入口：
 * 资产（调现金）/ 贷款（强制放贷、免债）/ 账号（封禁、管理员）/ 称号。
 * 统一查看用户、持仓、记录和批量操作。
 */
import { computed, h, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  useMessage, useDialog, NDataTable, NButton, NInput, NTabs, NTabPane, NSelect, NTag, NDrawer, NDrawerContent, NCheckbox,
  type DataTableColumns,
} from 'naive-ui'
import {
  adminUsersApi, adminTitleApi,
  type UserListItem, type AdminUserSummary, type UserTitleItem, type AdminUserActivity, type AdminUserListFilters,
} from '@/api/admin'
import type { TitleRead } from '@/api/title'
import { useAuthStore } from '@/stores/auth'
import { extractErrorMessage } from '@/utils/errors'
import AccountBatchPanel from '@/components/admin/AccountBatchPanel.vue'
import UserActivityTabs from '@/components/admin/UserActivityTabs.vue'
import BatchAdjustCashPanel from './batch/BatchAdjustCashPanel.vue'
import AmnestyPanel from './batch/AmnestyPanel.vue'
import TitleChip from '@/components/title/TitleChip.vue'

const message = useMessage()
const dialog = useDialog()
const authStore = useAuthStore()
const route = useRoute()
const router = useRouter()

// ── 列表 ──
const PAGE_SIZE = 100
const users = ref<UserListItem[]>([])
const allTitles = ref<TitleRead[]>([])
const listLoading = ref(false)
const moreLoading = ref(false)
const hasMore = ref(false)
const search = ref('')
const idInput = ref('')
const activeFilter = ref<boolean | null>(null)
const positionsFilter = ref<boolean | null>(null)
const shortsFilter = ref<boolean | null>(null)
const includeBots = ref(false)
const checkedIds = ref<number[]>([])
const batchBusy = ref(false)
const tool = ref<string>(route.query.tool === 'amnesty' ? 'amnesty' : route.query.tool === 'cash' ? 'cash' : 'accounts')
const detailOpen = ref(false)
const activity = ref<AdminUserActivity | null>(null)
const activityError = ref('')
const activeOptions = [{ label: '全部', value: 'all' }, { label: '正常', value: 'yes' }, { label: '已封禁', value: 'no' }]
const triOptions = [{ label: '全部', value: 'all' }, { label: '是', value: 'yes' }, { label: '否', value: 'no' }]
const parseIds = computed(() => {
  const ids = new Set<number>()
  if (!idInput.value.trim()) return { ids: [], error: '' }
  for (const token of idInput.value.split(/[,，\s]+/).filter(Boolean)) {
    const match = token.match(/^(\d+)(?:-(\d+))?$/)
    if (!match) return { ids: [], error: `无法识别“${token}”，请使用 ID 或起止范围` }
    const start = Number(match[1]), end = Number(match[2] ?? match[1])
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start <= 0 || end > 2147483647 || end < start) return { ids: [], error: `无效范围：${token}` }
    if (end - start >= 100) return { ids: [], error: '一次最多筛选 100 个 ID' }
    for (let id = start; id <= end; id++) ids.add(id)
    if (ids.size > 100) return { ids: [], error: '一次最多筛选 100 个 ID' }
  }
  return { ids: [...ids], error: '' }
})
function listFilters(): AdminUserListFilters {
  return { user_ids: parseIds.value.ids.length ? parseIds.value.ids.join(',') : undefined, is_active: activeFilter.value ?? undefined, has_positions: positionsFilter.value ?? undefined, has_shorts: shortsFilter.value ?? undefined, include_bots: includeBots.value }
}
function triValue(v: boolean | null) { return v === null ? 'all' : v ? 'yes' : 'no' }
function triBool(v: string) { return v === 'all' ? null : v === 'yes' }
function updateChecked(keys: Array<string | number>) {
  if (batchBusy.value) return
  if (keys.length > 100) { message.warning('一次最多选择 100 人'); return }
  checkedIds.value = keys.map(Number)
}

// 搜索走服务端（用户名模糊 / 精确 ID），避免只过滤已加载的前 N 人
let listSeq = 0
let searchTimer: ReturnType<typeof setTimeout> | null = null

async function loadList() {
  const seq = ++listSeq
  if (parseIds.value.error) { users.value = []; hasMore.value = false; listLoading.value = false; return }
  listLoading.value = true
  try {
    const [u, t] = await Promise.all([
      adminUsersApi.list(0, PAGE_SIZE, search.value.trim() || undefined, listFilters()),
      allTitles.value.length ? Promise.resolve(allTitles.value) : adminTitleApi.listTitles(),
    ])
    if (seq !== listSeq) return
    users.value = u
    checkedIds.value = checkedIds.value.filter(id => u.some(row => row.id === id))
    hasMore.value = u.length === PAGE_SIZE
    allTitles.value = t
  } catch (e) {
    if (seq === listSeq) message.error(extractErrorMessage(e, '加载失败'))
  } finally {
    if (seq === listSeq) listLoading.value = false
  }
}

async function loadMore() {
  if (moreLoading.value || !hasMore.value) return
  moreLoading.value = true
  const seq = listSeq
  try {
    const next = await adminUsersApi.list(
      users.value.length, PAGE_SIZE, search.value.trim() || undefined, listFilters(),
    )
    if (seq !== listSeq) return
    const seen = new Set(users.value.map(u => u.id))
    users.value = [...users.value, ...next.filter(u => !seen.has(u.id))]
    hasMore.value = next.length === PAGE_SIZE
  } catch (e) {
    message.error(extractErrorMessage(e, '加载更多失败'))
  } finally {
    moreLoading.value = false
  }
}

watch([search, idInput, activeFilter, positionsFilter, shortsFilter, includeBots], () => {
  checkedIds.value = []
  ++listSeq
  users.value = []
  hasMore.value = false
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => { void loadList() }, 300)
})

onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
})

// ── 详情 ──
const selectedId = ref<number | null>(null)
const summary = ref<AdminUserSummary | null>(null)
const userTitles = ref<UserTitleItem[]>([])
const detailLoading = ref(false)
const isSelf = computed(() => selectedId.value !== null && selectedId.value === authStore.user?.id)

let detailSeq = 0
async function loadDetail() {
  if (selectedId.value === null) return
  const seq = ++detailSeq
  const id = selectedId.value
  activity.value = null
  activityError.value = ''
  detailLoading.value = true
  try {
    const [s, t] = await Promise.all([
      adminUsersApi.get(id),
      adminTitleApi.listUserTitles(id),
    ])
    if (seq !== detailSeq) return
    summary.value = s
    userTitles.value = t
    try {
      const a = await adminUsersApi.activity(id)
      if (seq === detailSeq) activity.value = a
    } catch (e) {
      if (seq === detailSeq) activityError.value = extractErrorMessage(e, '加载持仓与记录失败')
    }
  } catch (e) {
    message.error(extractErrorMessage(e, '加载用户详情失败'))
  } finally {
    if (seq === detailSeq) detailLoading.value = false
  }
}

function select(id: number) {
  detailOpen.value = true
  selectedId.value = id
  summary.value = null
  userTitles.value = []
  router.replace({ query: { ...route.query, uid: String(id) } })
  loadDetail()
}

/** 任一操作成功后：刷新详情 + 列表里的那一行 */
async function afterMutation() {
  const hadSelection = checkedIds.value.length > 0
  checkedIds.value = []
  await Promise.all([loadDetail(), loadList()])
  if (hadSelection) message.info('列表已刷新，已清空选择。操作结果保留在下方。')
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
  { type: 'selection', disabled: () => batchBusy.value },
  { title: 'ID', key: 'id', width: 52 },
  { title: '用户名', key: 'username', minWidth: 140, render: r => h(NButton, { text: true, onClick: () => select(r.id) }, () => r.username) },
  { title: '现货 / 空头 / 预测', key: 'positions', width: 150, render: r => `${r.spot_count ?? 0} / ${r.short_count ?? 0} / ${r.lmsr_count ?? 0}` },
  { title: '最近交易', key: 'last_trade_at', width: 170, render: r => r.last_trade_at ? new Date(r.last_trade_at).toLocaleString('zh-CN') : '—' },
  { title: '现金', key: 'cash', width: 84, align: 'right', render: r => r.cash.toFixed(2) },
  { title: '债务', key: 'debt', width: 84, align: 'right', render: r => r.debt.toFixed(2) },
  {
    title: '', key: '_status', width: 88,
    render(r) {
      return h('div', { class: 'flex gap-1' }, [
        r.is_active ? null : h(NTag, { type: 'error', size: 'small' }, () => '封禁'),
        r.is_bot ? h(NTag, { size: 'small' }, () => '机器人') : null,
        r.is_superuser ? h(NTag, { type: 'warning', size: 'small' }, () => '管理员') : null,
      ])
    },
  },
]
const rowProps = (row: UserListItem) => ({
  style: 'cursor:pointer',
  class: row.id === selectedId.value ? 'row-selected' : '',
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
        <p class="page-sub">搜索并勾选用户，查看持仓与记录，预览后执行批量操作。点击用户名查看详情。</p>
      </div>

    </header>

    <div class="split">
      <section class="card list-card">
        <fieldset :disabled="batchBusy" class="filters">
          <NInput v-model:value="search" :disabled="batchBusy" placeholder="搜索用户名 / 精确 ID" clearable />
          <NInput v-model:value="idInput" :disabled="batchBusy" placeholder="ID 列表或范围，例如 641,646,672-677" clearable />
          <label>账号状态 <NSelect :value="triValue(activeFilter)" :options="activeOptions" :disabled="batchBusy" @update:value="v => activeFilter = triBool(v)" /></label>
          <label>有持仓 <NSelect :value="triValue(positionsFilter)" :options="triOptions" :disabled="batchBusy" @update:value="v => positionsFilter = triBool(v)" /></label>
          <label>有空头 <NSelect :value="triValue(shortsFilter)" :options="triOptions" :disabled="batchBusy" @update:value="v => shortsFilter = triBool(v)" /></label>
          <NCheckbox v-model:checked="includeBots" :disabled="batchBusy">包括官方机器人</NCheckbox>
        </fieldset>
        <p v-if="parseIds.error" class="filter-error">{{ parseIds.error }}</p>
        <NDataTable
          :columns="columns" :data="users" :loading="listLoading" :checked-row-keys="checkedIds" @update:checked-row-keys="updateChecked" :scroll-x="960"
          :bordered="true" size="small" :max-height="560"
          :row-key="(r: UserListItem) => r.id" :row-props="rowProps"
        />
        <div class="list-foot">
          <span>已加载 {{ users.length }} 人</span>
          <button
            v-if="hasMore" class="btn-outline btn-sm" :disabled="moreLoading || batchBusy"
            @click="loadMore"
          >{{ moreLoading ? '加载中…' : `加载更多 ${PAGE_SIZE} 人` }}</button>
        </div>
      </section>

      <section class="card tools-card">
        <div class="tool-bar"><strong>已选择 {{ checkedIds.length }} 人</strong><NButton size="small" :disabled="batchBusy || !checkedIds.length" @click="checkedIds = []">清空选择</NButton></div>
        <NTabs v-model:value="tool" type="line">
          <NTabPane name="accounts" tab="账户操作" :disabled="batchBusy"><AccountBatchPanel :user-ids="checkedIds" @busy="batchBusy = $event" @changed="afterMutation" /></NTabPane>
          <NTabPane name="cash" tab="批量调整现金" :disabled="batchBusy"><BatchAdjustCashPanel :user-ids="checkedIds.length ? checkedIds : undefined" @busy="batchBusy = $event" @changed="afterMutation" /></NTabPane>
          <NTabPane name="amnesty" tab="大赦天下" :disabled="batchBusy"><AmnestyPanel :user-ids="checkedIds.length ? checkedIds : undefined" @busy="batchBusy = $event" @changed="afterMutation" /></NTabPane>
        </NTabs>
      </section>
      <NDrawer v-model:show="detailOpen" :width="900" placement="right" :style="{ maxWidth: '100vw' }">
      <NDrawerContent title="用户详情" closable>
      <section class="detail-card">
        <div v-if="selectedId === null" class="empty-state">选择用户名查看详情</div>
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
              <span class="kv-label">金币借款债务（含已结利息）</span>
              <span class="kv-value tabular-nums">金 {{ summary.debt.toFixed(2) }}</span>
            </div>
            <div class="kv-item">
              <span class="kv-label">上次结息</span>
              <span class="kv-value">{{ summary.debt_last_accrued_at ? new Date(summary.debt_last_accrued_at).toLocaleString('zh-CN') : '—' }}</span>
            </div>
          </div>

          <NTabs :key="selectedId ?? 0" type="line" default-value="overview" size="small">
            <NTabPane name="overview" tab="概况与记录"><UserActivityTabs :activity="activity" :error="activityError" /></NTabPane>
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
                <p class="form-hint">受站点贷款开关约束；现金与债务同时增加。</p>
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
      </NDrawerContent></NDrawer>
    </div>
  </div>
</template>

<style scoped>
.filters {
  border: 0;
  padding: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 12px;
  margin-bottom: 14px;
  align-items: end;
}
@media (min-width: 768px) {
  .filters {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
  .filters > :nth-child(-n + 2) {
    grid-column: span 2;
  }
}
.filters label{font-size:12px}.filter-error{color:#b91c1c;font-size:13px}.tool-bar{display:flex;align-items:center;gap:16px;margin-bottom:10px}
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
@media (width > 950px) {
  .split {
    grid-template-columns: minmax(0, 1fr);
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
  display: flex;
  align-items: center;
  gap: 12px;
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
