<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { redemptionApi } from '@/api/redemption'
import { useUserStore } from '@/stores/user'
import { extractErrorMessage } from '@/utils/errors'
import type { BatchDetail, PurchaseResponse } from '@/types/redemption'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const batch = ref<BatchDetail | null>(null)
const showConfirm = ref(false)
const result = ref<PurchaseResponse | null>(null)
const error = ref<string>('')
const loading = ref(false)
const summaryLoading = ref(false)
const summaryReady = ref(false)
const summaryError = ref('')
const debtRule = '请先还清全部借款（含利息），再购买或兑换激活码'
const hasOutstandingDebt = computed(() => (userStore.summary?.debt ?? 0) > 0)
const paymentAllowed = computed(() => !summaryLoading.value && summaryReady.value
  && userStore.summary !== null && Number.isFinite(userStore.summary.debt) && userStore.summary.debt <= 0)
const canPurchase = computed(() => paymentAllowed.value && !loading.value
  && batch.value !== null && batch.value.available_count > 0)

const batchId = Number(route.params.id)

async function load() {
  try {
    batch.value = await redemptionApi.batchDetail(batchId)
  } catch (e) {
    error.value = extractErrorMessage(e, '加载失败')
  }
}

async function refreshSummary() {
  if (summaryLoading.value) return
  summaryLoading.value = true
  summaryReady.value = false
  summaryError.value = ''
  try {
    const summary = await userStore.fetchSummary()
    if (!summary || !Number.isFinite(summary.debt) || !Number.isFinite(summary.cash)) {
      summaryError.value = '无法确认账户余额与借款状态，请重试后再购买。'
      return
    }
    summaryReady.value = true
  } catch {
    summaryError.value = '无法确认账户余额与借款状态，请重试后再购买。'
  } finally {
    summaryLoading.value = false
  }
}

function openConfirm() {
  if (canPurchase.value) showConfirm.value = true
}

async function confirmPurchase() {
  if (!canPurchase.value) return
  loading.value = true
  error.value = ''
  try {
    result.value = await redemptionApi.purchase(batchId)
    showConfirm.value = false
    // 刷新用户余额
    await refreshSummary()
  } catch (e) {
    const failure = extractErrorMessage(e, '购买失败')
    error.value = failure === 'OUTSTANDING_DEBT' ? debtRule : failure
    if (typeof e === 'object' && e !== null && 'status' in e && e.status === 403) {
      await refreshSummary()
    }
  } finally {
    loading.value = false
  }
}

async function copyCode() {
  if (!result.value) return
  await navigator.clipboard.writeText(result.value.code_string)
  alert('已复制')
}

onMounted(() => {
  void load()
  void refreshSummary()
})
</script>

<template>
  <div class="page">
    <button class="back" @click="router.back()">← 返回</button>
    <p class="repayment-rule">
      存在任何未偿还借款（含利息）时，不能购买或兑换激活码。请先还清全部借款。
      <router-link to="/loan">前往借款页还款 →</router-link>
    </p>
    <div v-if="summaryLoading || !summaryReady" class="account-state" role="status">
      <p>{{ summaryLoading ? '正在确认账户余额与借款状态…' : summaryError || '尚未确认账户状态，暂不能购买。' }}</p>
      <button class="btn-secondary" :disabled="summaryLoading || loading" @click="refreshSummary">重试账户状态</button>
    </div>
    <p v-else-if="hasOutstandingDebt" class="account-state" role="alert">
      当前仍有未偿还借款（含利息）。{{ debtRule }}。
      <router-link to="/loan">去还款 →</router-link>
    </p>

    <div v-if="!batch && !error" class="loading">加载中…</div>
    <div v-if="error && !result" class="error">{{ error }}</div>

    <!-- 购买成功后展示码 -->
    <section v-if="result" class="result-card">
      <h2>购买成功</h2>
      <p class="hint">请复制下面的码，前往合作方站点核销。本码在「我的兑换」中可随时查看。</p>
      <div class="code-box">{{ result.code_string }}</div>
      <div class="action-row">
        <button class="btn-primary" @click="copyCode">复制</button>
        <a v-if="result.partner_website_url" :href="result.partner_website_url" target="_blank" rel="noopener" class="btn-secondary">
          前往 {{ result.partner_name }} →
        </a>
        <button class="btn-secondary" @click="router.push('/my/redemptions')">我的兑换</button>
      </div>
    </section>

    <!-- 详情 -->
    <section v-else-if="batch" class="detail-card">
      <h1>{{ batch.name }}</h1>
      <div class="meta">
        <span>合作方：{{ batch.partner.name }}</span>
        <span>价格：<b>{{ batch.unit_price }}</b></span>
        <span>剩余：{{ batch.available_count }}</span>
      </div>
      <pre class="description">{{ batch.description }}</pre>
      <p class="disclaimer">
        <span class="warning-tag">注意</span>
        兑换由 <b>{{ batch.partner.name }}</b> 独立履约。本站不参与核销，
        对合作方失约/商品争议不承担责任。
      </p>
      <button class="btn-primary" :disabled="!canPurchase" @click="openConfirm">
        {{ batch.available_count <= 0 ? '已售罄' : '购买' }}
      </button>
    </section>

    <!-- 二次确认弹窗 -->
    <div v-if="showConfirm" class="modal-bg" @click.self="!loading && (showConfirm = false)">
      <div class="modal-panel max-w-[480px]">
        <h3>确认购买</h3>
        <p>将扣除 <b>{{ batch?.unit_price }}</b> 资金购买「{{ batch?.name }}」。</p>
        <p class="warning"><span class="warning-tag">注意</span>码一旦显示视同交付，<b>不可退款</b>。请确认。</p>
        <p class="repayment-rule">{{ debtRule }}。<router-link to="/loan">去还款 →</router-link></p>
        <p v-if="summaryLoading || !summaryReady" class="account-state">
          {{ summaryLoading ? '正在确认账户状态…' : summaryError || '尚未确认账户状态，暂不能购买。' }}
          <button class="btn-secondary" :disabled="summaryLoading || loading" @click="refreshSummary">重试账户状态</button>
        </p>
        <div class="modal-actions">
          <button class="btn-secondary" @click="showConfirm = false" :disabled="loading">取消</button>
          <button class="btn-primary" @click="confirmPurchase" :disabled="!canPurchase">
            {{ loading ? '处理中…' : '确认' }}
          </button>
        </div>
        <p v-if="error" class="error">{{ error }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { padding: 16px; max-width: 720px; margin: 0 auto; }
.back { background: none; border: none; cursor: pointer; padding: 8px 0; font-size: 13px; }
.repayment-rule, .account-state { border: 2px solid #000; padding: 10px 14px; margin: 12px 0; background: #f5f5f5; font-size: 13px; line-height: 1.6; }
.repayment-rule a, .account-state a { color: #000; font-weight: 700; text-decoration: underline; white-space: nowrap; }
.account-state button { margin-top: 8px; }
.loading, .error { padding: 32px; text-align: center; }
.error { color: #dc2626; }
.detail-card, .result-card {
  border: 2px solid #000; padding: 24px; background: #fff;
  box-shadow: 6px 6px 0 #000;
}
.detail-card h1, .result-card h2 { font-size: 20px; font-weight: 700; margin-bottom: 12px; }
.meta {
  display: flex; gap: 16px; margin-bottom: 16px; font-size: 14px; flex-wrap: wrap;
  font-variant-numeric: tabular-nums;
}
.description {
  white-space: pre-wrap; font-family: inherit;
  padding: 12px; background: #f5f5f5; border: 1px solid #ddd;
  margin: 12px 0;
}
.code-box {
  font-family: monospace; font-size: 18px; padding: 16px; border: 2px solid #000;
  margin: 16px 0; background: #fafafa; word-break: break-all;
}
.hint { color: #666; font-size: 13px; margin: 8px 0; }
.warning { color: #dc2626; font-size: 13px; margin: 8px 0; }
.warning-tag {
  display: inline-block;
  padding: 0 6px;
  margin-right: 4px;
  border: 1.5px solid #b45309;
  background: #b45309;
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  vertical-align: 1px;
}
.disclaimer {
  border: 2px solid #000; padding: 10px 14px; margin: 12px 0;
  background: #fef2f2; font-size: 13px; color: #000;
}
.modal-actions { display: flex; gap: 8px; margin-top: 16px; justify-content: flex-end; }
.modal-panel h3 { font-size: 18px; margin-bottom: 12px; font-weight: 700; }
.action-row { display: flex; gap: 8px; flex-wrap: wrap; }
</style>
