<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { NInput, NButton, NSpin, NAlert, NDivider, useMessage } from 'naive-ui'
import { useLoanStore } from '@/stores/loan'

const store = useLoanStore()
const msg = useMessage()

const borrowAmount = ref('')
const repayAmount = ref('')
const submitting = ref(false)

onMounted(() => store.refresh())

const dailyRatePct = computed(() => {
  const r = store.quota?.daily_rate
  if (!r) return '—'
  return (Number(r) * 100).toFixed(2) + '%'
})

const debtNumber = computed(() => Number(store.quota?.debt ?? '0'))

async function submitBorrow() {
  if (!borrowAmount.value) return
  submitting.value = true
  try {
    await store.borrow(borrowAmount.value)
    msg.success(`借入 ${borrowAmount.value}`)
    borrowAmount.value = ''
  } catch (e: any) {
    msg.error(e?.message ?? '借款失败')
  } finally {
    submitting.value = false
  }
}

async function submitRepay() {
  if (!repayAmount.value) return
  submitting.value = true
  try {
    await store.repay(repayAmount.value)
    msg.success(`还款 ${repayAmount.value}`)
    repayAmount.value = ''
  } catch (e: any) {
    msg.error(e?.message ?? '还款失败')
  } finally {
    submitting.value = false
  }
}

function repayAll() {
  if (!store.quota) return
  repayAmount.value = store.quota.debt
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
          <NInput
            v-model:value="borrowAmount"
            placeholder="金额"
            :disabled="!store.quota?.enabled"
          />
          <NButton
            type="primary"
            :loading="submitting"
            :disabled="!store.quota?.enabled"
            @click="submitBorrow"
          >借入</NButton>
        </div>
      </section>

      <section class="panel">
        <h3>还款</h3>
        <div class="row">
          <NInput v-model:value="repayAmount" placeholder="金额" />
          <NButton :loading="submitting" @click="submitRepay">还款</NButton>
          <NButton
            quaternary
            :disabled="!store.quota || debtNumber <= 0"
            @click="repayAll"
          >全部还清</NButton>
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
.row {
  display: flex;
  gap: 8px;
  align-items: center;
}
h2, h3 {
  margin: 0 0 8px 0;
}
</style>
