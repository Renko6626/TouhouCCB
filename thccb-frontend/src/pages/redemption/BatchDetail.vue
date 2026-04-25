<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { redemptionApi } from '@/api/redemption'
import { useUserStore } from '@/stores/user'
import type { BatchDetail, PurchaseResponse } from '@/types/redemption'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const batch = ref<BatchDetail | null>(null)
const showConfirm = ref(false)
const result = ref<PurchaseResponse | null>(null)
const error = ref<string>('')
const loading = ref(false)

const batchId = Number(route.params.id)

async function load() {
  try {
    batch.value = await redemptionApi.batchDetail(batchId)
  } catch (e: any) {
    error.value = e?.message || '加载失败'
  }
}

async function confirmPurchase() {
  loading.value = true
  error.value = ''
  try {
    result.value = await redemptionApi.purchase(batchId)
    showConfirm.value = false
    // 刷新用户余额
    await userStore.fetchSummary()
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || '购买失败'
  } finally {
    loading.value = false
  }
}

async function copyCode() {
  if (!result.value) return
  await navigator.clipboard.writeText(result.value.code_string)
  alert('已复制')
}

onMounted(load)
</script>

<template>
  <div class="page">
    <button class="back" @click="router.back()">← 返回</button>

    <div v-if="!batch && !error" class="loading">加载中…</div>
    <div v-if="error && !result" class="error">{{ error }}</div>

    <!-- 购买成功后展示码 -->
    <section v-if="result" class="result-card">
      <h2>购买成功</h2>
      <p class="hint">请复制下面的码，前往合作方站点核销。本码在「我的兑换」中可随时查看。</p>
      <div class="code-box">{{ result.code_string }}</div>
      <button class="btn-primary" @click="copyCode">复制</button>
      <a v-if="result.partner_website_url" :href="result.partner_website_url" target="_blank" rel="noopener" class="btn-secondary">
        前往 {{ result.partner_name }} →
      </a>
      <button class="btn-secondary" @click="router.push('/my/redemptions')">我的兑换</button>
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
      <button class="btn-primary" :disabled="batch.available_count <= 0" @click="showConfirm = true">
        {{ batch.available_count <= 0 ? '已售罄' : '购买' }}
      </button>
    </section>

    <!-- 二次确认弹窗 -->
    <div v-if="showConfirm" class="modal-bg" @click.self="showConfirm = false">
      <div class="modal">
        <h3>确认购买</h3>
        <p>将扣除 <b>{{ batch?.unit_price }}</b> 资金购买「{{ batch?.name }}」。</p>
        <p class="warning">⚠ 码一旦显示视同交付，<b>不可退款</b>。请确认。</p>
        <div class="modal-actions">
          <button class="btn-secondary" @click="showConfirm = false" :disabled="loading">取消</button>
          <button class="btn-primary" @click="confirmPurchase" :disabled="loading">
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
.btn-primary {
  background: #000; color: #fff; border: 2px solid #000; padding: 10px 24px;
  cursor: pointer; font-weight: 600; margin-right: 8px; font-family: inherit;
  box-shadow: 4px 4px 0 #444;
  transition: transform 0.1s, box-shadow 0.1s;
}
.btn-primary:hover:not(:disabled) {
  transform: translate(-1px, -1px); box-shadow: 5px 5px 0 #444;
}
.btn-primary:disabled { opacity: 0.4; cursor: not-allowed; box-shadow: none; }
.btn-secondary {
  background: #fff; color: #000; border: 2px solid #000; padding: 10px 24px;
  cursor: pointer; font-weight: 600; margin-right: 8px; text-decoration: none;
  display: inline-block; font-family: inherit;
  box-shadow: 2px 2px 0 #000;
  transition: transform 0.1s, box-shadow 0.1s;
}
.btn-secondary:hover {
  transform: translate(-1px, -1px); box-shadow: 3px 3px 0 #000;
}
.code-box {
  font-family: monospace; font-size: 18px; padding: 16px; border: 2px dashed #000;
  margin: 16px 0; background: #fafafa; word-break: break-all;
}
.hint { color: #666; font-size: 13px; margin: 8px 0; }
.warning { color: #dc2626; font-size: 13px; margin: 8px 0; }
.modal-bg {
  position: fixed; inset: 0; background: rgba(0,0,0,0.4);
  display: flex; align-items: center; justify-content: center; z-index: 1000;
  padding: 16px;
}
.modal {
  background: #fff; border: 3px solid #000; padding: 24px;
  max-width: 480px; width: 100%; box-shadow: 6px 6px 0 #000;
}
.modal h3 { font-size: 18px; margin-bottom: 12px; font-weight: 700; }
.modal-actions { display: flex; gap: 8px; margin-top: 16px; justify-content: flex-end; }
</style>
