<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { danmukuApi, type DanmukuExchangeHistoryItem, type DanmukuExchangeResponse } from '@/api/danmuku'
import { useUserStore } from '@/stores/user'
import { extractErrorMessage } from '@/utils/errors'
import { useMessage } from 'naive-ui'

const userStore = useUserStore()
const message = useMessage()

const qqUserId = ref('')
const roomId = ref('弹幕群')
const yuan = ref<string>('')
const huo = ref<string>('')

const submitting = ref(false)
const error = ref('')
const showConfirm = ref(false)
const result = ref<DanmukuExchangeResponse | null>(null)

const history = ref<DanmukuExchangeHistoryItem[]>([])
const historyLoading = ref(false)

// 解析数字（空 → 0），失败 → NaN
const parseNum = (s: string): number => {
  const t = s.trim()
  if (t === '') return 0
  const n = Number(t)
  return Number.isFinite(n) ? n : NaN
}

const yuanNum = computed(() => parseNum(yuan.value))
const huoNum = computed(() => parseNum(huo.value))
const totalAmount = computed(() => {
  if (!Number.isFinite(yuanNum.value) || !Number.isFinite(huoNum.value)) return NaN
  return yuanNum.value + huoNum.value
})

const cash = computed(() => userStore.summary?.cash ?? 0)
const cashAfter = computed(() => cash.value - totalAmount.value)

const formValid = computed(() => {
  if (!qqUserId.value.trim()) return false
  if (!roomId.value.trim()) return false
  if (!Number.isFinite(yuanNum.value) || yuanNum.value < 0) return false
  if (!Number.isFinite(huoNum.value) || huoNum.value < 0) return false
  if (totalAmount.value <= 0) return false
  if (totalAmount.value > cash.value) return false
  return true
})

const formError = computed(() => {
  if (!qqUserId.value.trim()) return '请填写 QQ 号'
  if (!roomId.value.trim()) return '请填写房间号'
  if (!Number.isFinite(yuanNum.value)) return 'yuan 不是合法数字'
  if (!Number.isFinite(huoNum.value)) return 'huo 不是合法数字'
  if (yuanNum.value < 0 || huoNum.value < 0) return '金额不能为负'
  if (totalAmount.value <= 0) return 'yuan 与 huo 至少有一项为正'
  if (totalAmount.value > cash.value) return `余额不足（需 ${totalAmount.value.toFixed(2)}，仅有 ${cash.value.toFixed(2)}）`
  return ''
})

async function loadHistory() {
  historyLoading.value = true
  try {
    history.value = await danmukuApi.myHistory()
  } catch (e) {
    console.error('[Danmuku] history load failed:', e)
  } finally {
    historyLoading.value = false
  }
}

async function onExchange() {
  if (!formValid.value || submitting.value) return
  submitting.value = true
  error.value = ''
  try {
    const resp = await danmukuApi.exchange({
      qq_user_id: qqUserId.value.trim(),
      room_id: roomId.value.trim(),
      yuan: yuanNum.value,
      huo: huoNum.value,
    })
    result.value = resp
    showConfirm.value = false
    // 刷新余额 + 历史
    await Promise.all([userStore.fetchSummary(), loadHistory()])
    // 兑换成功后清空金额字段（QQ/房间号保留方便连续兑换）
    yuan.value = ''
    huo.value = ''
  } catch (e) {
    error.value = extractErrorMessage(e, '兑换失败')
  } finally {
    submitting.value = false
  }
}

async function copyCode(text: string) {
  await navigator.clipboard.writeText(text)
  message.success('激活码已复制')
}

onMounted(() => {
  if (!userStore.summary) userStore.fetchSummary()
  loadHistory()
})
</script>

<template>
  <div class="page">
    <header class="page-header">
      <h1 class="page-title">弹幕系统兑换</h1>
      <p class="page-sub">用站内现金按 1:1 兑换弹幕系统的 yuan / huo 激活码</p>
    </header>

    <div class="layout">
      <!-- 兑换表单 -->
      <section class="form-card">
        <h2 class="card-title">兑换表单</h2>

        <div class="balance-row">
          <span class="balance-label">当前现金</span>
          <span class="balance-value tabular-nums">¥ {{ cash.toFixed(2) }}</span>
        </div>

        <div class="form-grid">
          <label class="form-field">
            <span class="form-label">QQ 号（弹幕系统 user_id）</span>
            <input
              v-model="qqUserId"
              type="text"
              inputmode="numeric"
              placeholder="例如 123456789"
              :disabled="submitting"
              class="form-input"
              maxlength="32"
            />
          </label>

          <label class="form-field">
            <span class="form-label">房间号（room_id）</span>
            <input
              v-model="roomId"
              type="text"
              placeholder="弹幕群"
              :disabled="submitting"
              class="form-input"
              maxlength="64"
            />
          </label>

          <label class="form-field">
            <span class="form-label">Yuan 数量</span>
            <input
              v-model="yuan"
              type="text"
              inputmode="decimal"
              placeholder="0"
              :disabled="submitting"
              class="form-input tabular-nums"
            />
          </label>

          <label class="form-field">
            <span class="form-label">Huo 数量</span>
            <input
              v-model="huo"
              type="text"
              inputmode="decimal"
              placeholder="0"
              :disabled="submitting"
              class="form-input tabular-nums"
            />
          </label>
        </div>

        <div class="preview">
          <div class="preview-row">
            <span class="preview-label">合计扣减</span>
            <span class="preview-value tabular-nums">¥ {{ Number.isFinite(totalAmount) ? totalAmount.toFixed(2) : '—' }}</span>
          </div>
          <div class="preview-row preview-row--secondary">
            <span class="preview-label">兑换后余额</span>
            <span
              class="preview-value tabular-nums"
              :class="{ 'preview-value--bad': Number.isFinite(cashAfter) && cashAfter < 0 }"
            >
              ¥ {{ Number.isFinite(cashAfter) ? cashAfter.toFixed(2) : '—' }}
            </span>
          </div>
        </div>

        <p v-if="formError && (yuan || huo || qqUserId)" class="form-warning">
          <span class="warning-tag">注意</span>{{ formError }}
        </p>

        <button
          class="btn-primary"
          :disabled="!formValid || submitting"
          @click="showConfirm = true"
        >
          {{ submitting ? '处理中…' : '兑换' }}
        </button>
      </section>

      <!-- 历史记录 -->
      <section class="history-card">
        <h2 class="card-title">兑换历史</h2>

        <div v-if="historyLoading" class="history-empty">加载中…</div>
        <div v-else-if="history.length === 0" class="history-empty">尚无兑换记录</div>
        <ul v-else class="history-list">
          <li v-for="item in history" :key="item.id" class="history-item">
            <div class="history-meta">
              <span class="history-ts">{{ new Date(item.timestamp).toLocaleString('zh-CN') }}</span>
              <span class="history-amount tabular-nums">¥ {{ Number(item.amount).toFixed(2) }}</span>
            </div>
            <div class="history-detail">
              <span class="history-tag">QQ {{ item.qq_user_id }}</span>
              <span class="history-tag">房间 {{ item.room_id }}</span>
              <span class="history-tag tabular-nums">y {{ Number(item.yuan).toFixed(2) }}</span>
              <span class="history-tag tabular-nums">h {{ Number(item.huo).toFixed(2) }}</span>
            </div>
            <div class="history-code-row">
              <code class="history-code">{{ item.code_string }}</code>
              <button class="history-copy" @click="copyCode(item.code_string)" title="复制激活码">复制</button>
            </div>
          </li>
        </ul>
      </section>
    </div>

    <!-- 二次确认弹窗 -->
    <div v-if="showConfirm" class="modal-bg" @click.self="showConfirm = false">
      <div class="modal-panel">
        <h3 class="modal-title">确认兑换</h3>
        <p class="modal-text">
          将扣除现金 <b class="tabular-nums">¥ {{ totalAmount.toFixed(2) }}</b>，生成激活码并发送至
          <b>{{ qqUserId }}</b>（房间 <b>{{ roomId }}</b>）。
        </p>
        <p class="modal-warn">
          <span class="warning-tag">注意</span>码一旦生成视同交付，<b>不可退款</b>。
        </p>
        <div class="modal-actions">
          <button class="btn-secondary" @click="showConfirm = false" :disabled="submitting">取消</button>
          <button class="btn-primary" @click="onExchange" :disabled="submitting">
            {{ submitting ? '处理中…' : '确认' }}
          </button>
        </div>
        <p v-if="error" class="modal-error">{{ error }}</p>
      </div>
    </div>

    <!-- 兑换成功后高亮显示新生成的激活码 -->
    <div v-if="result" class="modal-bg" @click.self="result = null">
      <div class="modal-panel modal-panel--result">
        <h3 class="modal-title">兑换成功</h3>
        <p class="modal-text">
          已扣 <b class="tabular-nums">¥ {{ Number(result.amount).toFixed(2) }}</b>。
          请将激活码复制给弹幕系统侧的兑换流程。
        </p>
        <div class="code-box">{{ result.code_string }}</div>
        <div class="modal-actions">
          <button class="btn-secondary" @click="result = null">关闭</button>
          <button class="btn-primary" @click="copyCode(result.code_string)">复制激活码</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page {
  padding: 16px 0;
  max-width: 1120px;
  margin: 0 auto;
}

.page-header {
  margin-bottom: 20px;
}
.page-title {
  font-size: 22px;
  font-weight: 800;
  letter-spacing: 0.02em;
  color: #000;
}
.page-sub {
  margin-top: 6px;
  font-size: 13px;
  color: #555;
}

.layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
}
@media (min-width: 960px) {
  .layout {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  }
}

.form-card,
.history-card {
  border: 2px solid #000;
  background: #fff;
  padding: 20px;
  box-shadow: 6px 6px 0 #000;
}

.card-title {
  font-size: 14px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #000;
  margin-bottom: 14px;
  padding-bottom: 8px;
  border-bottom: 2px solid #000;
}

.balance-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: 8px 12px;
  background: #f5f5f5;
  border: 1px solid #000;
  margin-bottom: 16px;
}
.balance-label {
  font-size: 12px;
  color: #666;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.balance-value {
  font-size: 16px;
  font-weight: 700;
  color: #000;
}

.form-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 12px;
  margin-bottom: 16px;
}
@media (min-width: 540px) {
  .form-grid {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  }
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.form-label {
  font-size: 12px;
  font-weight: 600;
  color: #333;
}
.form-input {
  padding: 8px 10px;
  border: 2px solid #000;
  background: #fff;
  font-size: 14px;
  font-family: inherit;
}
.form-input:focus {
  outline: none;
  background: #fafafa;
  box-shadow: 2px 2px 0 #000;
}
.form-input:disabled {
  background: #f0f0f0;
  color: #888;
}

.preview {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px;
  background: #fafafa;
  border: 1px solid #000;
  margin-bottom: 14px;
}
.preview-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}
.preview-row--secondary {
  font-size: 12px;
  color: #666;
}
.preview-label {
  font-size: 13px;
  color: #333;
}
.preview-value {
  font-size: 15px;
  font-weight: 700;
  color: #000;
}
.preview-value--bad {
  color: #dc2626;
}

.form-warning {
  font-size: 12px;
  color: #444;
  margin-bottom: 12px;
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
  text-transform: uppercase;
}

.btn-primary,
.btn-secondary {
  padding: 9px 24px;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.04em;
  border: 2px solid #000;
  cursor: pointer;
  font-family: inherit;
  transition: transform 0.1s, box-shadow 0.1s;
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
.btn-primary:hover:not(:disabled),
.btn-secondary:hover:not(:disabled) {
  transform: translate(-1px, -1px);
  box-shadow: 5px 5px 0 #444;
}
.btn-primary:disabled,
.btn-secondary:disabled {
  background: #ddd;
  border-color: #ddd;
  color: #888;
  box-shadow: 4px 4px 0 #eee;
  cursor: not-allowed;
}

/* 历史列表 */
.history-empty {
  padding: 24px;
  text-align: center;
  font-size: 13px;
  color: #888;
}
.history-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: 560px;
  overflow-y: auto;
}
.history-item {
  border: 1px solid #000;
  background: #fff;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.history-meta {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: 12px;
}
.history-ts {
  color: #666;
}
.history-amount {
  font-weight: 700;
  color: #000;
}
.history-detail {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.history-tag {
  font-size: 11px;
  padding: 1px 6px;
  background: #f0f0f0;
  border: 1px solid #ccc;
  color: #333;
}
.history-code-row {
  display: flex;
  gap: 6px;
  align-items: center;
}
.history-code {
  flex: 1 1 auto;
  display: block;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-size: 11px;
  padding: 6px 8px;
  background: #f5f5f5;
  border: 1px solid #ccc;
  overflow-x: auto;
  white-space: nowrap;
}
.history-copy {
  flex: 0 0 auto;
  padding: 4px 10px;
  font-size: 11px;
  font-weight: 700;
  background: #fff;
  border: 1.5px solid #000;
  cursor: pointer;
}
.history-copy:hover {
  background: #000;
  color: #fff;
}

/* 弹窗 */
.modal-bg {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  z-index: 9000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px 16px;
}
.modal-panel {
  width: 100%;
  max-width: 480px;
  background: #fff;
  border: 4px solid #000;
  box-shadow: 8px 8px 0 #000;
  padding: 20px 22px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.modal-panel--result {
  max-width: 560px;
}
.modal-title {
  font-size: 16px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.modal-text {
  font-size: 13px;
  line-height: 1.6;
  color: #222;
}
.modal-warn {
  font-size: 12px;
  color: #444;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 4px;
}
.modal-error {
  font-size: 12px;
  color: #dc2626;
  font-weight: 600;
}

.code-box {
  padding: 12px;
  background: #f5f5f5;
  border: 1.5px solid #000;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-size: 12px;
  word-break: break-all;
  line-height: 1.5;
  max-height: 200px;
  overflow-y: auto;
}
</style>
