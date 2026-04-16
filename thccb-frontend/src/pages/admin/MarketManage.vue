<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import {
  NButton,
  NDataTable,
  NDatePicker,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NModal,
  NSpace,
  NTag,
  useDialog,
  useMessage,
  type DataTableColumns,
} from 'naive-ui'
import type { MarketDetail, MarketListItem } from '@/types/api'
import { useMarketStore } from '@/stores/market'
import { marketApi } from '@/api/market'
import { adminApi, type UserListItem } from '@/api/admin'

const message = useMessage()
const dialog = useDialog()
const marketStore = useMarketStore()

const loading = ref(false)
const searchQuery = ref('')

// 统计概览
const statsCards = computed(() => [
  { label: '交易中市场', value: marketStore.markets.length },
  { label: '注册用户', value: userList.value.length },
  { label: '管理员', value: userList.value.filter(u => u.is_superuser).length },
])
const showCreateModal = ref(false)
const showSettleModal = ref(false)

let outcomeIdCounter = 0
const makeOutcome = (label: string) => ({ id: ++outcomeIdCounter, label })

const createForm = ref({
  title: '',
  description: '',
  liquidity_b: 100,
  outcomes: [makeOutcome('是'), makeOutcome('否')],
  closes_at: null as number | null,  // NDatePicker 返回 timestamp
  tagsInput: '',  // 逗号分隔输入
})
const creating = ref(false)
const createError = ref('')

const settleMarketId = ref<number | null>(null)
const settleMarketTitle = ref('')
const settleOutcomes = ref<Array<{ id: number; label: string }>>([])
const settleWinningOutcomeId = ref<number | null>(null)
const settling = ref(false)

// 用户管理
const userList = ref<UserListItem[]>([])
const userLoading = ref(false)
const cashForm = ref({ userId: null as number | null, amount: 0, reason: '' })
const cashRunning = ref(false)

const loadUsers = async () => {
  userLoading.value = true
  try {
    userList.value = await adminApi.listUsers()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '加载用户列表失败')
  } finally {
    userLoading.value = false
  }
}

const submitAdjustCash = async () => {
  if (!cashForm.value.userId) { message.error('请选择用户'); return }
  if (cashForm.value.amount === 0) { message.error('金额不能为 0'); return }

  const action = cashForm.value.amount > 0 ? '加钱' : '扣钱'
  const confirmed = await new Promise<boolean>((resolve) => {
    dialog.warning({
      title: `确认${action}`,
      content: `确认给用户 #${cashForm.value.userId} ${action} ¥${Math.abs(cashForm.value.amount)}？`,
      positiveText: '确认',
      negativeText: '取消',
      onPositiveClick: () => resolve(true),
      onNegativeClick: () => resolve(false),
    })
  })
  if (!confirmed) return

  cashRunning.value = true
  try {
    const result = await adminApi.adjustCash(cashForm.value.userId, cashForm.value.amount, cashForm.value.reason)
    message.success(`${action}成功：${result.username} 当前现金 ¥${result.new_cash}`)
    cashForm.value = { userId: null, amount: 0, reason: '' }
    await loadUsers()
  } catch (error: any) {
    message.error(error?.message || `${action}失败`)
  } finally {
    cashRunning.value = false
  }
}

const userColumns: DataTableColumns<UserListItem> = [
  { title: 'ID', key: 'id', width: 60 },
  { title: '用户名', key: 'username' },
  { title: '现金', key: 'cash', width: 120, render: (row) => `¥${row.cash.toFixed(2)}` },
  { title: '负债', key: 'debt', width: 100, render: (row) => `¥${row.debt.toFixed(2)}` },
  {
    title: '角色', key: 'is_superuser', width: 80,
    render: (row) => h(NTag, { type: row.is_superuser ? 'warning' : 'default', size: 'small' }, { default: () => row.is_superuser ? '管理员' : '用户' }),
  },
  {
    title: '操作', key: 'actions', width: 100,
    render: (row) => h(NButton, { size: 'small', onClick: () => { cashForm.value.userId = row.id } }, { default: () => '调整现金' }),
  },
]

const directOps = ref({
  marketId: null as number | null,
  winningOutcomeId: null as number | null,
})
const directRunning = ref(false)

const filteredMarkets = computed(() => {
  const query = searchQuery.value.trim().toLowerCase()
  if (!query) return marketStore.markets
  return marketStore.markets.filter((m) =>
    m.title.toLowerCase().includes(query) || (m.description || '').toLowerCase().includes(query),
  )
})

const loadMarkets = async () => {
  loading.value = true
  try {
    await marketStore.fetchMarkets()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '加载市场失败')
  } finally {
    loading.value = false
  }
}

const resetCreateForm = () => {
  createForm.value = { title: '', description: '', liquidity_b: 100, outcomes: [makeOutcome('是'), makeOutcome('否')], closes_at: null, tagsInput: '' }
  createError.value = ''
}

const handleCreateSubmit = async () => {
  if (!createForm.value.title.trim()) { createError.value = '请输入市场标题'; return }
  const outcomes = createForm.value.outcomes.map((o) => o.label.trim()).filter(Boolean)
  if (outcomes.length < 2) { createError.value = '至少提供两个有效选项'; return }

  creating.value = true
  createError.value = ''
  try {
    const tags = createForm.value.tagsInput
      ? createForm.value.tagsInput.split(',').map(t => t.trim()).filter(Boolean)
      : undefined
    const closes_at = createForm.value.closes_at
      ? new Date(createForm.value.closes_at).toISOString()
      : undefined
    const result = await marketStore.createMarket({
      title: createForm.value.title,
      description: createForm.value.description,
      liquidity_b: createForm.value.liquidity_b,
      outcomes,
      tags,
      closes_at,
    })
    if (!result.success) throw new Error(result.error || '创建失败')
    showCreateModal.value = false
    resetCreateForm()
    message.success('市场创建成功')
    await loadMarkets()
  } catch (error) {
    createError.value = error instanceof Error ? error.message : '创建市场失败'
  } finally {
    creating.value = false
  }
}

const closeMarket = async (market: MarketListItem) => {
  const confirmed = await new Promise<boolean>((resolve) => {
    dialog.warning({
      title: '确认熔断',
      content: `确认暂停市场「${market.title}」吗？`,
      positiveText: '确认',
      negativeText: '取消',
      onPositiveClick: () => resolve(true),
      onNegativeClick: () => resolve(false),
    })
  })
  if (!confirmed) return
  try {
    const result = await marketStore.closeMarket(market.id)
    if (!result.success) throw new Error(result.error || '熔断失败')
    message.success('市场已熔断')
    await loadMarkets()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '熔断失败')
  }
}

const openSettleModal = async (marketId: number, marketTitle?: string) => {
  try {
    const detail: MarketDetail = await marketApi.getMarketDetail(marketId)
    settleMarketId.value = marketId
    settleMarketTitle.value = marketTitle || detail.title
    settleOutcomes.value = detail.outcomes.map((o) => ({ id: o.id, label: o.label }))
    settleWinningOutcomeId.value = settleOutcomes.value[0]?.id ?? null
    showSettleModal.value = true
  } catch (error) {
    message.error(error instanceof Error ? error.message : '加载结算选项失败')
  }
}

const closeAndSettle = async (market: MarketListItem) => {
  try {
    const closeResult = await marketStore.closeMarket(market.id)
    if (!closeResult.success) throw new Error(closeResult.error || '熔断失败')
    message.success('已先熔断市场，请选择赢家进行结算')
    await openSettleModal(market.id, market.title)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '熔断并结算流程失败')
  }
}

const submitSettle = async () => {
  if (!settleMarketId.value || !settleWinningOutcomeId.value) { message.error('请选择获胜选项'); return }

  // 结算不可撤销，二次确认
  const confirmed = await new Promise<boolean>((resolve) => {
    dialog.warning({
      title: '确认结算',
      content: `确认将市场 #${settleMarketId.value} 结算为选项 #${settleWinningOutcomeId.value} 获胜？此操作不可撤销！`,
      positiveText: '确认结算',
      negativeText: '取消',
      onPositiveClick: () => resolve(true),
      onNegativeClick: () => resolve(false),
      onClose: () => resolve(false),
    })
  })
  if (!confirmed) return

  settling.value = true
  try {
    const result = await marketStore.settleMarket(settleMarketId.value, settleWinningOutcomeId.value)
    if (!result.success) throw new Error(result.error || '结算失败')
    showSettleModal.value = false
    message.success('市场结算成功')
    await loadMarkets()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '市场结算失败')
  } finally {
    settling.value = false
  }
}

const directResume = async () => {
  if (!directOps.value.marketId) { message.error('请输入市场ID'); return }
  directRunning.value = true
  try {
    const result = await marketStore.resumeMarket(directOps.value.marketId)
    if (!result.success) throw new Error(result.error || '恢复失败')
    message.success('市场已恢复交易')
    await loadMarkets()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '恢复失败')
  } finally {
    directRunning.value = false
  }
}

const directSettle = async () => {
  if (!directOps.value.marketId || !directOps.value.winningOutcomeId) {
    message.error('请输入市场ID和赢家选项ID')
    return
  }

  const confirmed = await new Promise<boolean>((resolve) => {
    dialog.warning({
      title: '确认按ID结算',
      content: `确认将市场 #${directOps.value.marketId} 结算为选项 #${directOps.value.winningOutcomeId} 获胜？此操作不可撤销！`,
      positiveText: '确认结算',
      negativeText: '取消',
      onPositiveClick: () => resolve(true),
      onNegativeClick: () => resolve(false),
      onClose: () => resolve(false),
    })
  })
  if (!confirmed) return

  directRunning.value = true
  try {
    const result = await marketStore.settleMarket(directOps.value.marketId, directOps.value.winningOutcomeId)
    if (!result.success) throw new Error(result.error || '结算失败')
    message.success('按ID结算成功')
    await loadMarkets()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '按ID结算失败')
  } finally {
    directRunning.value = false
  }
}

const columns: DataTableColumns<MarketListItem> = [
  { title: 'ID', key: 'id', width: 80, render: (row) => `#${row.id}` },
  { title: '市场标题', key: 'title', render: (row) => row.title },
  { title: '流动性', key: 'liquidity_b', width: 120, render: (row) => `¥${row.liquidity_b.toLocaleString()}` },
  {
    title: '状态', key: 'status', width: 120, render: (row) => {
      const map: Record<string, string> = { trading: '交易中', halt: '已熔断', settled: '已结算' }
      return h(NTag, { type: 'default', size: 'small' }, { default: () => map[row.status] ?? row.status })
    }
  },
  {
    title: '操作',
    key: 'actions',
    width: 260,
    render: (row) =>
      h(NSpace, {}, {
        default: () => [
          h(NButton, { size: 'small', onClick: () => closeMarket(row) }, { default: () => '熔断' }),
          h(NButton, { size: 'small', onClick: () => closeAndSettle(row) }, { default: () => '熔断并结算' }),
        ],
      }),
  },
]

onMounted(() => {
  loadMarkets()
  loadUsers()
})
</script>

<template>
  <div class="manage-page">
    <!-- 页头 -->
    <div class="page-bar">
      <div class="page-bar-left">
        <span class="page-bar-title">管理后台</span>
        <span class="page-bar-sub">市场管理 · 用户管理 · 平台概览</span>
      </div>
      <NButton @click="showCreateModal = true">
        <template #icon><i class="i-mdi-plus-circle"></i></template>
        创建新市场
      </NButton>
    </div>

    <!-- 统计概览 -->
    <div class="stats-bar">
      <div v-for="card in statsCards" :key="card.label" class="stats-item">
        <span class="stats-label">{{ card.label }}</span>
        <span class="stats-value">{{ card.value }}</span>
      </div>
    </div>

    <!-- 搜索 -->
    <div class="content-panel">
      <p class="panel-note">当前 /market/list 仅返回交易中市场；已熔断市场请使用下方"按市场ID操作"。</p>
      <div class="row-gap">
        <NInput v-model:value="searchQuery" placeholder="按市场标题搜索..." clearable style="flex:1" />
        <NButton :loading="loading" @click="loadMarkets">刷新</NButton>
      </div>
    </div>

    <!-- 市场列表 -->
    <div class="content-panel">
      <div class="panel-heading">交易中市场 ({{ filteredMarkets.length }})</div>
      <NDataTable :columns="columns" :data="filteredMarkets" :loading="loading" :bordered="false" size="small" />
    </div>

    <!-- 按ID操作 -->
    <div class="content-panel">
      <div class="panel-heading">按市场ID操作（适用于已熔断市场）</div>
      <div class="row-gap">
        <NInputNumber v-model:value="directOps.marketId" :min="1" placeholder="市场ID" style="width:160px" />
        <NInputNumber v-model:value="directOps.winningOutcomeId" :min="1" placeholder="赢家选项ID（结算用）" style="width:220px" />
        <NButton :loading="directRunning" @click="directResume">按ID恢复</NButton>
        <NButton :loading="directRunning" @click="directSettle">按ID结算</NButton>
      </div>
    </div>

    <!-- 用户管理 -->
    <div class="content-panel">
      <div class="panel-heading">用户管理</div>
      <div class="row-gap" style="margin-bottom:12px;">
        <NInputNumber v-model:value="cashForm.userId" :min="1" placeholder="用户ID" style="width:120px" size="small" />
        <NInputNumber v-model:value="cashForm.amount" placeholder="金额（正=加，负=扣）" style="width:200px" size="small" />
        <NInput v-model:value="cashForm.reason" placeholder="原因（可选）" style="width:160px" size="small" />
        <NButton size="small" :loading="cashRunning" @click="submitAdjustCash">执行</NButton>
        <NButton size="small" :loading="userLoading" @click="loadUsers">刷新</NButton>
      </div>
      <NDataTable :columns="userColumns" :data="userList" :loading="userLoading" :bordered="false" size="small" :max-height="300" />
    </div>

    <!-- 创建市场弹窗 -->
    <NModal v-model:show="showCreateModal" preset="card" title="创建新市场" style="width:90%;max-width:640px">
      <NForm :model="createForm">
        <NFormItem label="市场标题" required>
          <NInput v-model:value="createForm.title" placeholder="请输入市场标题" :disabled="creating" />
        </NFormItem>
        <NFormItem label="市场描述">
          <NInput v-model:value="createForm.description" type="textarea" :rows="3" placeholder="请输入市场描述" :disabled="creating" />
        </NFormItem>
        <NFormItem label="初始流动性" required>
          <NInputNumber v-model:value="createForm.liquidity_b" :min="1" :disabled="creating" style="width:100%" />
        </NFormItem>
        <NFormItem label="交易截止时间">
          <NDatePicker
            v-model:value="createForm.closes_at"
            type="datetime"
            clearable
            :disabled="creating"
            placeholder="留空则无截止时间"
            style="width: 100%"
          />
        </NFormItem>
        <NFormItem label="标签">
          <NInput v-model:value="createForm.tagsInput" placeholder="用逗号分隔，如：东方,体育,政治" :disabled="creating" />
        </NFormItem>
        <NFormItem label="市场选项" required>
          <div class="outcomes-editor">
            <div v-for="(item, idx) in createForm.outcomes" :key="item.id" class="row-gap">
              <NInput v-model:value="item.label" placeholder="选项名称" :disabled="creating" style="flex:1" />
              <NButton v-if="createForm.outcomes.length > 2" size="small" :disabled="creating" @click="createForm.outcomes.splice(idx, 1)">删除</NButton>
            </div>
            <NButton size="small" :disabled="creating" @click="createForm.outcomes.push(makeOutcome(''))">添加选项</NButton>
          </div>
        </NFormItem>
        <div v-if="createError" class="form-error">{{ createError }}</div>
      </NForm>
      <template #footer>
        <div class="modal-footer">
          <NButton @click="showCreateModal = false">取消</NButton>
          <NButton type="primary" :loading="creating" @click="handleCreateSubmit">创建</NButton>
        </div>
      </template>
    </NModal>

    <!-- 结算弹窗 -->
    <NModal v-model:show="showSettleModal" preset="card" title="结算市场" style="width:90%;max-width:560px">
      <p class="settle-label">市场：{{ settleMarketTitle }}（ID: {{ settleMarketId }}）</p>
      <NForm>
        <NFormItem label="赢家选项" required>
          <NInputNumber v-model:value="settleWinningOutcomeId" :min="1" style="width:100%" placeholder="请输入赢家选项ID" />
        </NFormItem>
      </NForm>
      <div class="outcomes-ref">
        <div class="outcomes-ref-title">可选 outcome：</div>
        <div v-for="item in settleOutcomes" :key="item.id" class="outcomes-ref-item">#{{ item.id }} — {{ item.label }}</div>
      </div>
      <template #footer>
        <div class="modal-footer">
          <NButton @click="showSettleModal = false">取消</NButton>
          <NButton type="primary" :loading="settling" @click="submitSettle">确认结算</NButton>
        </div>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.manage-page {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.stats-bar {
  display: flex;
  gap: 0;
  border: 2px solid #000;
}

.stats-item {
  flex: 1;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  border-right: 1px solid #000;
}

.stats-item:last-child { border-right: none; }

.stats-label {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #888;
}

.stats-value {
  font-size: 24px;
  font-weight: 900;
  color: #000;
  font-variant-numeric: tabular-nums;
}

.panel-note {
  font-size: 12px;
  color: #666666;
  padding: 8px 12px;
  background: #f5f5f5;
  border: 1px solid #cccccc;
  margin-bottom: 12px;
}

.row-gap {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.outcomes-editor {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}

.settle-label {
  font-size: 13px;
  color: #444444;
  margin-bottom: 16px;
}

.outcomes-ref {
  background: #f5f5f5;
  border: 1px solid #cccccc;
  padding: 12px;
  margin-top: 12px;
}

.outcomes-ref-title {
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 8px;
}

.outcomes-ref-item {
  font-family: monospace;
  font-size: 13px;
  color: #333333;
  padding: 2px 0;
}
</style>
