<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { NInputNumber, NButton, NSpin, NAlert, NDivider, useMessage } from 'naive-ui'
import { useLoanStore } from '@/stores/loan'

const store = useLoanStore()
const msg = useMessage()

const borrowAmount = ref<number | null>(null)
const repayAmount = ref<number | null>(null)
const submitting = ref(false)

onMounted(() => store.refresh())

const dailyRatePct = computed(() => {
  const r = store.quota?.daily_rate
  if (!r) return '—'
  return (Number(r) * 100).toFixed(2) + '%'
})

const debtNumber = computed(() => Number(store.quota?.debt ?? '0'))
const cashNumber = computed(() => Number(store.quota?.cash ?? '0'))
const maxBorrowNumber = computed(() => Number(store.quota?.max_borrow ?? '0'))
// 还款上限：min(真实负债, 真实现金) — 与服务端封顶逻辑对齐
const maxRepayNumber = computed(() => Math.min(debtNumber.value, cashNumber.value))

// 用户输入超出实际能扣减部分时的预览
const repayOverflow = computed(() => {
  const v = Number(repayAmount.value ?? 0)
  const cap = maxRepayNumber.value
  if (v > 0 && v > cap) return v - cap
  return 0
})

async function submitBorrow() {
  if (!borrowAmount.value || borrowAmount.value <= 0) return
  submitting.value = true
  try {
    await store.borrow(String(borrowAmount.value))
    msg.success(`借入 ${borrowAmount.value}`)
    borrowAmount.value = null
  } catch (e: any) {
    msg.error(e?.data?.detail ?? e?.message ?? '借款失败')
  } finally {
    submitting.value = false
  }
}

async function submitRepay() {
  if (!repayAmount.value || repayAmount.value <= 0) return
  submitting.value = true
  try {
    const r = await store.repay(String(repayAmount.value))
    const eff = r.effective ? Number(r.effective) : Number(repayAmount.value)
    if (Math.abs(eff - Number(repayAmount.value)) > 0.001) {
      msg.success(`实际还款 ¥${eff.toFixed(2)}（输入 ¥${repayAmount.value} 已自动按真实负债 / 现金封顶）`)
    } else {
      msg.success(`还款 ¥${eff.toFixed(2)}`)
    }
    repayAmount.value = null
  } catch (e: any) {
    msg.error(e?.data?.detail ?? e?.message ?? '还款失败')
  } finally {
    submitting.value = false
  }
}

function repayAll() {
  // 还到能还的最大值：min(真实负债, 真实现金)
  if (maxRepayNumber.value <= 0) return
  repayAmount.value = maxRepayNumber.value
  submitRepay()
}
</script>

<template>
  <div class="loan-page">
    <NSpin :show="store.loading">
      <NAlert v-if="store.error" type="error" :title="store.error" />
      <NAlert
        v-else-if="store.quota && !store.quota.enabled"
        type="warning"
        title="借款功能已关闭"
      />

      <section class="panel">
        <h2>当前负债</h2>
        <div class="debt-number" :class="{ red: debtNumber > 0 }">
          {{ store.quota?.debt ?? '—' }}
        </div>
        <div class="meta">
          <span>可借额度：<strong>{{ store.quota?.max_borrow ?? '—' }}</strong></span>
          <span class="sep">·</span>
          <span>日利率：<strong>{{ dailyRatePct }}</strong></span>
          <span class="sep">·</span>
          <span>现金：<strong>{{ store.quota?.cash ?? '—' }}</strong></span>
        </div>
        <div class="meta-small" v-if="store.quota?.last_accrued_at">
          上次结息：{{ new Date(store.quota.last_accrued_at).toLocaleString() }}
        </div>
      </section>

      <NDivider />

      <section class="panel">
        <h3>借款</h3>
        <div class="row">
          <NInputNumber
            v-model:value="borrowAmount"
            placeholder="金额"
            :min="0.01"
            :max="maxBorrowNumber"
            :precision="2"
            :disabled="!store.quota?.enabled || maxBorrowNumber <= 0"
            style="width: 200px"
          />
          <NButton
            type="primary"
            :loading="submitting"
            :disabled="!store.quota?.enabled || !borrowAmount || borrowAmount <= 0"
            @click="submitBorrow"
          >借入</NButton>
        </div>
        <div v-if="store.quota?.enabled" class="meta-small">
          可借额度：<strong>¥{{ maxBorrowNumber.toFixed(2) }}</strong>
        </div>
      </section>

      <section class="panel">
        <h3>还款</h3>
        <div class="row">
          <NInputNumber
            v-model:value="repayAmount"
            placeholder="金额"
            :min="0.01"
            :precision="2"
            :disabled="debtNumber <= 0 || cashNumber <= 0"
            style="width: 200px"
          />
          <NButton
            :loading="submitting"
            :disabled="!repayAmount || repayAmount <= 0 || debtNumber <= 0"
            @click="submitRepay"
          >还款</NButton>
          <NButton
            quaternary
            :disabled="maxRepayNumber <= 0"
            @click="repayAll"
          >还到上限 ¥{{ maxRepayNumber.toFixed(2) }}</NButton>
        </div>
        <div v-if="repayOverflow > 0" class="meta-small warn">
          ⚠ 输入 ¥{{ repayAmount }} 超过可还上限 ¥{{ maxRepayNumber.toFixed(2) }}，
          实际只会扣减 ¥{{ maxRepayNumber.toFixed(2) }}（多出的 ¥{{ repayOverflow.toFixed(2) }} 不收取）
        </div>
        <div v-else-if="debtNumber > 0" class="meta-small">
          当前真实负债 <strong>¥{{ debtNumber.toFixed(2) }}</strong>，可用现金 <strong>¥{{ cashNumber.toFixed(2) }}</strong>，
          可还上限 <strong>¥{{ maxRepayNumber.toFixed(2) }}</strong>
        </div>
      </section>
    </NSpin>
  </div>
</template>

<style scoped>
.loan-page {
  padding: 16px;
  max-width: 640px;
}
.panel {
  margin-bottom: 16px;
  border: 2px solid #000;
  padding: 16px;
  background: #fff;
}
.debt-number {
  font-size: 40px;
  font-weight: 700;
}
.debt-number.red {
  color: #d14;
}
.meta {
  margin-top: 8px;
  color: #555;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 8px;
  align-items: baseline;
}
.sep {
  color: #bbb;
}
.meta-small {
  margin-top: 4px;
  font-size: 12px;
  color: #888;
}
.meta-small.warn {
  color: #b45309;
  font-weight: 600;
}
.meta-small strong {
  color: #000;
}
.row {
  display: flex;
  gap: 8px;
  align-items: center;
}
h2, h3 {
  margin: 0 0 8px 0;
}
</style>
