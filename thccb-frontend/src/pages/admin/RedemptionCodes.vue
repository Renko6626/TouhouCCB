<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NModal, useMessage } from 'naive-ui'
import { redemptionAdminApi } from '@/api/redemption'
import type {
  BatchAdminItem, RedemptionCodeAdminItem, RedemptionCodeFilter,
} from '@/types/redemption'
import { extractErrorMessage } from '@/utils/errors'

type CodeAction = { kind: 'redeem' | 'revoke'; item: RedemptionCodeAdminItem }

const route = useRoute()
const router = useRouter()
const message = useMessage()
const pageSize = 50
const items = ref<RedemptionCodeAdminItem[]>([])
const batches = ref<BatchAdminItem[]>([])
const batchId = ref<number | undefined>()
const searchInput = ref('')
const search = ref('')
const status = ref<RedemptionCodeFilter>('all')
const page = ref(1)
const total = ref(0)
const loading = ref(false)
const batchesLoading = ref(false)
const loadError = ref('')
const batchError = ref('')
const operationError = ref('')
const action = ref<CodeAction | null>(null)
const actionText = ref('')
const actionError = ref('')
const submitting = ref(false)
let requestId = 0
let disposed = false

const statuses: { value: RedemptionCodeFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'available', label: '未售出' },
  { value: 'pending', label: '待核销' },
  { value: 'redeemed', label: '已核销' },
]
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const firstItem = computed(() => total.value === 0 ? 0 : (page.value - 1) * pageSize + 1)
const lastItem = computed(() => Math.min(page.value * pageSize, total.value))
const unknownBatch = computed(() => batchId.value !== undefined && !batches.value.some(b => b.id === batchId.value))
const canConfirm = computed(() => !submitting.value && actionText.value.length <= 500
  && (action.value?.kind !== 'revoke' || (!!action.value.item.redeemed_at && actionText.value.trim().length > 0)))

function canRedeem(item: RedemptionCodeAdminItem) {
  return item.status === 'sold' && item.bought_by_user_id !== null && !item.redeemed_at
}

function statusLabel(item: RedemptionCodeAdminItem) {
  if (item.redeemed_at) return '已核销'
  if (item.status === 'available') return '未售出'
  return item.bought_by_user_id === null ? '购买人缺失' : '待核销'
}

function userLabel(username: string | null, id: number | null) {
  return username ?? (id === null ? '—' : `用户 #${id}`)
}

const fmtTime = (value: string | null) => value ? new Date(value).toLocaleString('zh-CN') : '—'

async function loadCodes() {
  const id = ++requestId
  loading.value = true
  loadError.value = ''
  try {
    const result = await redemptionAdminApi.listCodes({
      batch_id: batchId.value,
      q: search.value || undefined,
      status: status.value,
      page: page.value,
      page_size: pageSize,
    })
    if (id !== requestId || disposed) return
    const maxPage = Math.max(1, Math.ceil(result.total / pageSize))
    // 核销后当前筛选可能少一条；最后一页变空时回到仍存在的页。
    if (page.value > maxPage) {
      page.value = maxPage
      await loadCodes()
      return
    }
    items.value = result.items
    total.value = result.total
    page.value = result.page
  } catch (err) {
    if (id !== requestId || disposed) return
    items.value = []
    total.value = 0
    loadError.value = extractErrorMessage(err, '兑换码加载失败，请重试')
  } finally {
    if (id === requestId && !disposed) loading.value = false
  }
}

async function loadBatches() {
  if (batchesLoading.value) return
  batchesLoading.value = true
  batchError.value = ''
  try {
    const result = await redemptionAdminApi.listBatches()
    if (!disposed) batches.value = result
  } catch (err) {
    if (!disposed) batchError.value = extractErrorMessage(err, '批次筛选加载失败')
  } finally {
    if (!disposed) batchesLoading.value = false
  }
}

function applySearch() {
  if (submitting.value) return
  search.value = searchInput.value.trim()
  page.value = 1
  void loadCodes()
}

function changeStatus() {
  page.value = 1
  void loadCodes()
}

function changeBatch(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  void router.replace({ query: { ...route.query, batch_id: value || undefined } })
}

function changePage(next: number) {
  if (next < 1 || next > totalPages.value || loading.value || submitting.value) return
  page.value = next
  void loadCodes()
}

function openAction(kind: CodeAction['kind'], item: RedemptionCodeAdminItem) {
  if (loading.value || submitting.value || (kind === 'redeem' ? !canRedeem(item) : !item.redeemed_at)) return
  action.value = { kind, item }
  actionText.value = ''
  actionError.value = ''
  operationError.value = ''
}

function closeAction() {
  if (!submitting.value) action.value = null
}

async function confirmAction() {
  if (!action.value || !canConfirm.value) return
  const current = action.value
  submitting.value = true
  actionError.value = ''
  operationError.value = ''
  try {
    if (current.kind === 'redeem') {
      await redemptionAdminApi.redeemCode(current.item.id, actionText.value.trim())
    } else {
      if (!current.item.redeemed_at) return
      await redemptionAdminApi.revokeCode(current.item.id, actionText.value.trim(), current.item.redeemed_at)
    }
    if (disposed) return
    action.value = null
    await loadCodes()
    message.success(current.kind === 'redeem' ? '核销已确认' : '核销已撤销')
  } catch (err) {
    if (disposed) return
    actionError.value = extractErrorMessage(err, '操作失败，请重试')
    operationError.value = actionError.value
    if (typeof err === 'object' && err !== null && 'status' in err && err.status === 409) {
      action.value = null
      await loadCodes()
      message.warning(operationError.value)
    }
  } finally {
    if (!disposed) submitting.value = false
  }
}

watch(() => route.query.batch_id, value => {
  const raw = Array.isArray(value) ? value[0] : value
  const parsed = raw ? Number(raw) : undefined
  batchId.value = parsed !== undefined && Number.isSafeInteger(parsed) && parsed > 0 ? parsed : undefined
  page.value = 1
  void loadCodes()
}, { immediate: true })

onMounted(loadBatches)
onBeforeUnmount(() => {
  disposed = true
  requestId += 1
})
</script>

<template>
  <div class="page">
    <header class="page-header">
      <div>
        <h1 class="page-title">兑换码核销</h1>
        <p class="hint">查看全部兑换码；核对兑换码和购买人后，确认线下兑换。</p>
      </div>
      <button class="btn-secondary" :disabled="loading || submitting" @click="loadCodes">刷新</button>
    </header>

    <form class="filters" @submit.prevent="applySearch">
      <label class="search-field">
        兑换码搜索
        <input v-model="searchInput" type="search" placeholder="输入兑换码或其中一部分" :disabled="submitting" />
      </label>
      <button class="btn-primary search-button" type="submit" :disabled="submitting">搜索</button>
      <label>
        批次
        <select :value="batchId ?? ''" :disabled="submitting || batchesLoading" @change="changeBatch">
          <option value="">全部批次</option>
          <option v-if="unknownBatch" :value="batchId">批次 #{{ batchId }}</option>
          <option v-for="batch in batches" :key="batch.id" :value="batch.id">
            {{ batch.partner_name }} · {{ batch.name }}
          </option>
        </select>
      </label>
      <label>
        状态
        <select v-model="status" :disabled="submitting" @change="changeStatus">
          <option v-for="option in statuses" :key="option.value" :value="option.value">{{ option.label }}</option>
        </select>
      </label>
    </form>

    <p v-if="batchError" class="notice" role="alert">
      {{ batchError }}
      <button class="text-button" :disabled="batchesLoading" @click="loadBatches">重新加载批次</button>
    </p>
    <p v-if="operationError" class="notice" role="alert">{{ operationError }}</p>

    <div v-if="loading" class="state" role="status">加载兑换码中…</div>
    <div v-else-if="loadError" class="state error-state" role="alert">
      <p>{{ loadError }}</p>
      <button class="btn-secondary" :disabled="submitting" @click="loadCodes">重试</button>
    </div>
    <div v-else-if="items.length === 0" class="state">没有符合条件的兑换码。</div>
    <ul v-else class="code-list" aria-label="兑换码列表">
      <li v-for="item in items" :key="item.id" class="code-row">
        <div class="code-info">
          <div class="code-heading">
            <span class="status-tag" :class="{ 'status-tag--redeemed': !!item.redeemed_at }">{{ statusLabel(item) }}</span>
            <span class="hint">#{{ item.id }}</span>
          </div>
          <div class="code-value">{{ item.code_string }}</div>
          <p class="batch-name">{{ item.batch_name }}</p>
          <p class="hint">{{ item.partner_name }}</p>
        </div>
        <div class="record-info">
          <p><span class="record-label">购买人</span>{{ userLabel(item.bought_by_username, item.bought_by_user_id) }}</p>
          <p><span class="record-label">购买时间</span>{{ fmtTime(item.bought_at) }}</p>
          <template v-if="item.redeemed_at">
            <p><span class="record-label">核销管理员</span>{{ userLabel(item.redeemed_by_admin_username, item.redeemed_by_admin_id) }}</p>
            <p><span class="record-label">核销时间</span>{{ fmtTime(item.redeemed_at) }}</p>
          </template>
          <p v-if="item.redemption_note" class="record-note"><span class="record-label">核销备注</span>{{ item.redemption_note }}</p>
        </div>
        <div class="row-actions">
          <button v-if="item.redeemed_at" class="btn-secondary" :disabled="submitting" @click="openAction('revoke', item)">撤销核销</button>
          <button v-else class="btn-primary" :disabled="submitting || !canRedeem(item)" @click="openAction('redeem', item)">核销</button>
          <span v-if="!item.redeemed_at && !canRedeem(item)" class="hint">{{ item.status === 'available' ? '未售出，无法核销' : '购买人缺失，无法核销' }}</span>
        </div>
      </li>
    </ul>

    <nav v-if="!loading && !loadError && total > 0" class="pagination" aria-label="兑换码分页">
      <span class="hint">共 {{ total }} 条 · {{ firstItem }}–{{ lastItem }} · 每页 {{ pageSize }} 条</span>
      <div class="page-actions">
        <button class="btn-secondary" :disabled="page <= 1 || submitting" @click="changePage(page - 1)">上一页</button>
        <span class="page-number">{{ page }} / {{ totalPages }}</span>
        <button class="btn-secondary" :disabled="page >= totalPages || submitting" @click="changePage(page + 1)">下一页</button>
      </div>
    </nav>

    <NModal
      :show="action !== null"
      preset="card"
      :title="action?.kind === 'revoke' ? '撤销核销' : '确认核销'"
      style="width: calc(100% - 32px); max-width: 560px"
      :mask-closable="!submitting"
      :close-on-esc="!submitting"
      :closable="!submitting"
      @update:show="closeAction"
    >
      <form v-if="action" class="action-form" @submit.prevent="confirmAction">
        <p>{{ action.kind === 'redeem' ? '确认已向购买人完成线下兑换？' : '撤销后，此码将恢复待核销状态。' }}</p>
        <div class="confirmation-code">{{ action.item.code_string }}</div>
        <p>{{ action.item.partner_name }} · {{ action.item.batch_name }}</p>
        <p>购买人：{{ userLabel(action.item.bought_by_username, action.item.bought_by_user_id) }}</p>
        <label for="action-note">{{ action.kind === 'redeem' ? '核销备注（选填）' : '撤销原因（必填）' }}</label>
        <textarea
          id="action-note"
          v-model="actionText"
          rows="4"
          maxlength="500"
          :required="action.kind === 'revoke'"
          :disabled="submitting"
          :placeholder="action.kind === 'redeem' ? '可填写发放内容或现场说明' : '请填写撤销原因'"
        ></textarea>
        <p class="hint note-count">{{ actionText.length }} / 500</p>
        <p v-if="actionError" class="notice" role="alert">{{ actionError }}</p>
        <div class="modal-actions">
          <button type="button" class="btn-secondary" :disabled="submitting" @click="closeAction">取消</button>
          <button class="btn-primary" type="submit" :disabled="!canConfirm">
            {{ submitting ? '提交中…' : action.kind === 'redeem' ? '确认核销' : '确认撤销' }}
          </button>
        </div>
      </form>
    </NModal>
  </div>
</template>

<style scoped>
.page { padding: 16px; max-width: 1200px; margin: 0 auto; }
.page-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
.page-title { font-size: 22px; font-weight: 800; }
.hint { color: #666; font-size: 12px; line-height: 1.6; }
.page-header .hint { margin-top: 4px; }
.filters { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 12px; padding: 16px; border: 2px solid #000; background: #f5f5f5; margin-bottom: 16px; }
.filters label { display: flex; flex-direction: column; gap: 6px; min-width: 120px; font-size: 12px; font-weight: 700; }
.search-field { flex: 1; min-width: 220px !important; }
input, select, textarea { border: 2px solid #000; background: #fff; color: #000; padding: 8px; font-family: inherit; font-size: 13px; border-radius: 0; }
.filters select { max-width: 280px; }
.btn-primary, .btn-secondary { display: inline-flex; align-items: center; justify-content: center; border: 2px solid #000; padding: 8px 14px; font-family: inherit; font-size: 13px; font-weight: 700; cursor: pointer; white-space: nowrap; }
.btn-primary { background: #000; color: #fff; box-shadow: 3px 3px 0 #555; }
.btn-secondary { background: #fff; color: #000; box-shadow: 2px 2px 0 #000; }
.btn-secondary:hover:not(:disabled) { background: #000; color: #fff; }
.btn-primary:hover:not(:disabled), .btn-secondary:hover:not(:disabled) { transform: translate(-1px, -1px); }
button:disabled, input:disabled, select:disabled, textarea:disabled { cursor: not-allowed; opacity: .5; }
button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible { outline: 2px solid #000; outline-offset: 3px; }
.notice { border: 2px solid #000; padding: 10px 12px; margin-bottom: 12px; background: #f0f0f0; font-size: 13px; overflow-wrap: anywhere; }
.text-button { background: none; border: 0; text-decoration: underline; font-family: inherit; cursor: pointer; margin-left: 8px; }
.state { padding: 48px 16px; border: 2px solid #ccc; text-align: center; font-size: 14px; color: #666; }
.error-state .btn-secondary { margin-top: 16px; }
.code-list { margin: 0; padding: 0; list-style: none; }
.code-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 140px; gap: 20px; padding: 16px; margin-bottom: 16px; border: 2px solid #000; background: #fff; box-shadow: 4px 4px 0 #000; }
.code-heading { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }
.status-tag { border: 1.5px solid #000; padding: 2px 8px; font-size: 11px; font-weight: 700; }
.status-tag--redeemed { background: #000; color: #fff; }
.code-value, .confirmation-code { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 15px; font-weight: 700; overflow-wrap: anywhere; }
.batch-name { font-size: 13px; font-weight: 700; margin-top: 8px; overflow-wrap: anywhere; }
.record-info { font-size: 12px; line-height: 1.8; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.record-label { display: inline-block; color: #666; margin-right: 8px; }
.record-note { white-space: pre-wrap; }
.row-actions { display: flex; flex-direction: column; justify-content: center; align-items: stretch; gap: 8px; }
.pagination { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; margin-top: 20px; }
.page-actions { display: flex; align-items: center; gap: 12px; }
.page-number { font-size: 13px; font-weight: 700; font-variant-numeric: tabular-nums; }
.action-form { font-size: 13px; line-height: 1.6; }
.confirmation-code { border: 2px solid #000; padding: 10px; margin: 12px 0; }
.action-form label { display: block; margin: 16px 0 6px; font-weight: 700; }
.action-form textarea { width: 100%; resize: vertical; }
.note-count { text-align: right; }
.modal-actions { display: flex; gap: 12px; justify-content: flex-end; margin-top: 16px; }
@media (max-width: 720px) {
  .code-row { grid-template-columns: minmax(0, 1fr); gap: 12px; }
  .row-actions { align-items: flex-start; border-top: 1px solid #ccc; padding-top: 12px; }
  .filters { gap: 10px; }
  .filters label { flex: 1; min-width: 0; }
  .search-field { flex-basis: calc(100% - 90px) !important; min-width: 0 !important; }
  .filters select { max-width: 100%; width: 100%; }
  .search-button { flex-shrink: 0; }
  .pagination { justify-content: center; }
}
</style>
