import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import { loanApi, type LoanQuota, type LoanActionResult } from '@/api/loan'
import { useAuthStore } from '@/stores/auth'
import { extractErrorMessage } from '@/utils/errors'

export const useLoanStore = defineStore('loan', () => {
  const quota = ref<LoanQuota | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const auth = useAuthStore()
  let accountVersion = 0
  let refreshVersion = 0
  watch([() => auth.user?.id, () => auth.accessToken != null], () => {
    accountVersion++
    refreshVersion++
    quota.value = null
    error.value = null
    loading.value = false
  }, { flush: 'sync' })

  async function refreshQuota(afterOperation = false) {
    const version = ++refreshVersion
    quota.value = null
    loading.value = true
    error.value = null
    try {
      const snapshot = await loanApi.quota()
      if (version === refreshVersion) quota.value = snapshot
    } catch (e: unknown) {
      if (version === refreshVersion) {
        const detail = extractErrorMessage(e, '加载失败')
        error.value = afterOperation ? `操作已完成，账户信息刷新失败：${detail}` : detail
      }
    } finally {
      if (version === refreshVersion) loading.value = false
    }
  }

  async function refresh() {
    await refreshQuota()
  }

  async function perform(operation: () => Promise<LoanActionResult>) {
    const account = accountVersion
    const result = await operation()
    if (account === accountVersion) await refreshQuota(true)
    return result
  }

  async function borrow(amount: string) {
    return perform(() => loanApi.borrow(amount))
  }

  async function repay(amount: string) {
    return perform(() => loanApi.repay(amount))
  }

  async function repayAll() {
    return perform(() => loanApi.repayAll())
  }

  return { quota, loading, error, refresh, borrow, repay, repayAll }
})
