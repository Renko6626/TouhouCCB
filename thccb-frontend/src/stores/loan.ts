import { defineStore } from 'pinia'
import { ref } from 'vue'
import { loanApi, type LoanQuota } from '@/api/loan'
import { extractErrorMessage } from '@/utils/errors'

export const useLoanStore = defineStore('loan', () => {
  const quota = ref<LoanQuota | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function refresh() {
    quota.value = null
    loading.value = true
    error.value = null
    try {
      quota.value = await loanApi.quota()
    } catch (e: unknown) {
      error.value = extractErrorMessage(e, '加载失败')
    } finally {
      loading.value = false
    }
  }

  async function borrow(amount: string) {
    const r = await loanApi.borrow(amount)
    quota.value = null
    await refresh()
    return r
  }

  async function repay(amount: string) {
    const r = await loanApi.repay(amount)
    quota.value = null
    await refresh()
    return r
  }

  async function repayAll() {
    const r = await loanApi.repayAll()
    quota.value = null
    await refresh()
    return r
  }

  return { quota, loading, error, refresh, borrow, repay, repayAll }
})
