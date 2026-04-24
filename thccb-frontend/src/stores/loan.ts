import { defineStore } from 'pinia'
import { ref } from 'vue'
import { loanApi, type LoanQuota } from '@/api/loan'

export const useLoanStore = defineStore('loan', () => {
  const quota = ref<LoanQuota | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function refresh() {
    loading.value = true
    error.value = null
    try {
      quota.value = await loanApi.quota()
    } catch (e: any) {
      error.value = e?.message ?? '加载失败'
    } finally {
      loading.value = false
    }
  }

  async function borrow(amount: string) {
    const r = await loanApi.borrow(amount)
    if (quota.value) {
      quota.value.cash = r.cash
      quota.value.debt = r.debt
      quota.value.max_borrow = r.max_borrow
    }
    return r
  }

  async function repay(amount: string) {
    const r = await loanApi.repay(amount)
    if (quota.value) {
      quota.value.cash = r.cash
      quota.value.debt = r.debt
      quota.value.max_borrow = r.max_borrow
    }
    return r
  }

  return { quota, loading, error, refresh, borrow, repay }
})
