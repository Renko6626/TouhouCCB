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
  NSelect,
  NSpace,
  NTag,
  useDialog,
  useMessage,
  type DataTableColumns,
  type SelectOption,
} from 'naive-ui'
import type { MarketDetail, MarketListItem } from '@/types/api'
import { useMarketStore } from '@/stores/market'
import { marketApi } from '@/api/market'
import { adminTitleApi } from '@/api/admin'
import type { TitleRead } from '@/api/title'

const message = useMessage()
const dialog = useDialog()
const marketStore = useMarketStore()

const loading = ref(false)
const searchQuery = ref('')

// 统计概览
const statsCards = computed(() => [
  { label: '交易中市场', value: marketStore.markets.length },
  { label: '全部市场（含熔断/结算）', value: allMarkets.value.length },
])
const showCreateModal = ref(false)
const showSettleModal = ref(false)

let outcomeIdCounter = 0
// price: 初始价格百分比（先验）；null = 不设，全部为空则均匀 1/N
const makeOutcome = (label: string) => ({ id: ++outcomeIdCounter, label, price: null as number | null })

const createForm = ref({
  title: '',
  description: '',
  liquidity_b: 100,
  outcomes: [makeOutcome('是'), makeOutcome('否')],
  closes_at: null as number | null,  // NDatePicker 返回 timestamp
  tagsInput: '',  // 逗号分隔输入
  requiredTitleIds: [] as number[],  // 称号门槛（空 = 任何人可交易）
})
const creating = ref(false)
const createError = ref('')

// 称号门槛 - 创建表单 & 单市场编辑共享 options
const titleOptions = ref<{ label: string; value: number }[]>([])

async function loadTitleOptions() {
  try {
    const titles = await adminTitleApi.listTitles()
    titleOptions.value = titles
      .filter((t: TitleRead) => t.is_active)
      .map((t: TitleRead) => ({ label: t.name, value: t.id }))
  } catch {
    // 不阻塞主流程，多选下拉留空
  }
}

// 单市场「设置门槛」模态
const showGatingModal = ref(false)
const gatingMarketId = ref<number | null>(null)
const gatingMarketTitle = ref('')
const gatingSelectedIds = ref<number[]>([])
const gatingLoading = ref(false)

async function openGatingModal(row: MarketListItem) {
  gatingMarketId.value = row.id
  gatingMarketTitle.value = row.title
  gatingSelectedIds.value = []
  try {
    gatingSelectedIds.value = await adminTitleApi.getMarketRequired(row.id)
  } catch {
    gatingSelectedIds.value = []
  }
  showGatingModal.value = true
}

async function saveGating() {
  if (!gatingMarketId.value) return
  gatingLoading.value = true
  try {
    await adminTitleApi.putMarketRequired(gatingMarketId.value, gatingSelectedIds.value)
    message.success('已保存称号门槛')
    showGatingModal.value = false
  } catch (e) {
    message.error((e as { message?: string })?.message || '保存失败')
  } finally {
    gatingLoading.value = false
  }
}

const settleMarketId = ref<number | null>(null)
const settleMarketTitle = ref('')
const settleOutcomes = ref<Array<{ id: number; label: string }>>([])
const settleWinningOutcomeId = ref<number | null>(null)
const settling = ref(false)

const directOps = ref({
  marketId: null as number | null,
  winningOutcomeId: null as number | null,
})
const directRunning = ref(false)
const directOutcomes = ref<Array<{ id: number; label: string }>>([])

// 「按ID操作」面板：marketStore 只列 trading 状态市场，所以这里独立维护一份
// 全状态列表（含 halt）方便管理员选已熔断市场
const allMarkets = ref<Array<{ id: number; title: string; status: string }>>([])

const allMarketOptions = computed<SelectOption[]>(() =>
  allMarkets.value.map(m => ({
    label: `#${m.id}  ${m.title}  [${m.status}]`,
    value: m.id,
  })),
)

const directOutcomeOptions = computed<SelectOption[]>(() =>
  directOutcomes.value.map(o => ({ label: `#${o.id}  ${o.label}`, value: o.id })),
)

const settleOutcomeOptions = computed<SelectOption[]>(() =>
  settleOutcomes.value.map(o => ({ label: `#${o.id}  ${o.label}`, value: o.id })),
)

// marketId 变化时拉对应市场的 outcomes 给「按ID结算」用
const onDirectMarketChange = async (mid: number | null) => {
  directOps.value.winningOutcomeId = null
  directOutcomes.value = []
  if (!mid) return
  try {
    const detail: MarketDetail = await marketApi.getMarketDetail(mid)
    directOutcomes.value = detail.outcomes.map(o => ({ id: o.id, label: o.label }))
  } catch (error) {
    message.error(error instanceof Error ? error.message : '加载选项失败')
  }
}

const loadAllMarkets = async () => {
  try {
    const list = await marketApi.getMarkets({ include_halt: true, include_settled: true })
    allMarkets.value = list.map(m => ({ id: m.id, title: m.title, status: m.status }))
  } catch (error) {
    message.error(error instanceof Error ? error.message : '加载市场列表失败')
  }
}

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
  createForm.value = {
    title: '',
    description: '',
    liquidity_b: 100,
    outcomes: [makeOutcome('是'), makeOutcome('否')],
    closes_at: null,
    tagsInput: '',
    requiredTitleIds: [],
  }
  createError.value = ''
}

const handleCreateSubmit = async () => {
  if (!createForm.value.title.trim()) { createError.value = '请输入市场标题'; return }
  const validRows = createForm.value.outcomes.filter((o) => o.label.trim())
  const outcomes = validRows.map((o) => o.label.trim())
  if (outcomes.length < 2) { createError.value = '至少提供两个有效选项'; return }
  // 初始价格：要么全不填（均匀），要么全填且和为 100%
  const filled = validRows.filter((o) => o.price !== null)
  let initial_prices: number[] | undefined
  if (filled.length > 0) {
    if (filled.length !== validRows.length) { createError.value = '初始价格要么全部留空，要么每个选项都填'; return }
    if (validRows.some((o) => (o.price ?? 0) <= 0)) { createError.value = '初始价格必须大于 0'; return }
    const sum = validRows.reduce((a, o) => a + (o.price ?? 0), 0)
    if (Math.abs(sum - 100) > 0.5) { createError.value = `初始价格之和应为 100%，当前 ${sum.toFixed(2)}%`; return }
    initial_prices = validRows.map((o) => (o.price ?? 0) / 100)
  }

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
      initial_prices,
    })
    if (!result.success) throw new Error(result.error || '创建失败')

    // 创建成功后，如配置了称号门槛，调 PUT 写入；失败不阻断主流程
    const newMarketId = result.data?.market_id
    if (newMarketId && createForm.value.requiredTitleIds.length > 0) {
      try {
        await adminTitleApi.putMarketRequired(newMarketId, createForm.value.requiredTitleIds)
      } catch (e) {
        message.warning((e as { message?: string })?.message || '市场已创建，但称号门槛保存失败')
      }
    }

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
  { title: '流动性', key: 'liquidity_b', width: 120, render: (row) => `金 ${row.liquidity_b.toLocaleString()}` },
  {
    title: '状态', key: 'status', width: 120, render: (row) => {
      const map: Record<string, string> = { trading: '交易中', halt: '已熔断', settled: '已结算' }
      return h(NTag, { type: 'default', size: 'small' }, { default: () => map[row.status] ?? row.status })
    }
  },
  {
    title: '操作',
    key: 'actions',
    width: 360,
    render: (row) =>
      h(NSpace, { size: 6 }, {
        default: () => [
          h(NButton, { size: 'small', onClick: () => closeMarket(row) }, { default: () => '熔断' }),
          h(NButton, { size: 'small', onClick: () => closeAndSettle(row) }, { default: () => '熔断并结算' }),
          h(NButton, { size: 'small', onClick: () => openGatingModal(row) }, { default: () => '设置门槛' }),
        ],
      }),
  },
]

onMounted(() => {
  loadMarkets()
  loadAllMarkets()
  loadTitleOptions()
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
      <div class="panel-heading">按市场操作（含已熔断/已结算）</div>
      <div class="row-gap">
        <NSelect
          :value="directOps.marketId"
          :options="allMarketOptions"
          placeholder="选择市场"
          filterable
          clearable
          style="min-width:280px;flex:1;max-width:480px"
          @update:value="(v: number | null) => { directOps.marketId = v; onDirectMarketChange(v) }"
        />
        <NSelect
          v-model:value="directOps.winningOutcomeId"
          :options="directOutcomeOptions"
          placeholder="赢家选项（结算用）"
          :disabled="!directOps.marketId || directOutcomes.length === 0"
          clearable
          style="min-width:240px"
        />
        <NButton :loading="directRunning" :disabled="!directOps.marketId" @click="directResume">恢复交易</NButton>
        <NButton :loading="directRunning" :disabled="!directOps.marketId || !directOps.winningOutcomeId" @click="directSettle">结算</NButton>
        <NButton size="small" @click="loadAllMarkets">刷新</NButton>
      </div>
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
              <NInputNumber
                v-model:value="item.price"
                :min="0.01"
                :max="99.99"
                :step="1"
                :show-button="false"
                clearable
                placeholder="初始价 %"
                :disabled="creating"
                style="width:120px"
              />
              <NButton v-if="createForm.outcomes.length > 2" size="small" :disabled="creating" @click="createForm.outcomes.splice(idx, 1)">删除</NButton>
            </div>
            <NButton size="small" :disabled="creating" @click="createForm.outcomes.push(makeOutcome(''))">添加选项</NButton>
            <div class="outcomes-hint">初始价 % 为先验概率：全部留空则均匀 1/N；填写则需每项都填且合计 100%（q₀ = b·ln p）</div>
          </div>
        </NFormItem>
        <NFormItem label="需要的称号 (空 = 任何人可交易)">
          <NSelect
            v-model:value="createForm.requiredTitleIds"
            :options="titleOptions"
            multiple
            placeholder="选择需要的称号 (留空则任何人可交易)"
            clearable
            :disabled="creating"
            style="width:100%"
          />
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
          <NSelect
            v-model:value="settleWinningOutcomeId"
            :options="settleOutcomeOptions"
            placeholder="请选择赢家选项"
            style="width:100%"
          />
        </NFormItem>
      </NForm>
      <template #footer>
        <div class="modal-footer">
          <NButton @click="showSettleModal = false">取消</NButton>
          <NButton type="primary" :loading="settling" @click="submitSettle">确认结算</NButton>
        </div>
      </template>
    </NModal>

    <!-- 设置称号门槛 -->
    <NModal
      v-model:show="showGatingModal"
      preset="card"
      :title="`市场 #${gatingMarketId} 的称号门槛`"
      style="width:90%;max-width:500px"
    >
      <p class="settle-label">市场：{{ gatingMarketTitle }}（ID: {{ gatingMarketId }}）</p>
      <NForm>
        <NFormItem label="需要的称号 (留空则任何人可交易)">
          <NSelect
            v-model:value="gatingSelectedIds"
            :options="titleOptions"
            multiple
            placeholder="选择需要的称号"
            clearable
            style="width:100%"
          />
        </NFormItem>
      </NForm>
      <template #footer>
        <div class="modal-footer">
          <NButton @click="showGatingModal = false">取消</NButton>
          <NButton type="primary" :loading="gatingLoading" @click="saveGating">保存</NButton>
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

.outcomes-hint {
  font-size: 12px;
  color: var(--text-muted, #888);
  line-height: 1.4;
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
