import { ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { useAuthStore } from './auth'

export const useTradingPreferencesStore = defineStore('tradingPreferences', () => {
  const auth = useAuthStore()
  const confirmBorrow = ref(true)
  const key = () => (auth.user?.id ? `trading-preferences:${auth.user.id}:confirm-borrow` : null)
  watch(
    () => auth.user?.id,
    () => {
      confirmBorrow.value = true
      try {
        const storageKey = key()
        if (storageKey) confirmBorrow.value = localStorage.getItem(storageKey) !== 'false'
      } catch {
        /* Storage restrictions retain the default confirmation. */
      }
    },
    { immediate: true, flush: 'sync' },
  )
  function setConfirmBorrow(enabled: boolean): boolean {
    const storageKey = key()
    if (!storageKey) return false
    try {
      localStorage.setItem(storageKey, String(enabled))
      confirmBorrow.value = enabled
      return true
    } catch {
      confirmBorrow.value = true
      return false
    }
  }
  return { confirmBorrow, setConfirmBorrow }
})
