import { beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { reactive } from 'vue'
const { identity } = vi.hoisted(() => ({ identity: { user: { id: 1 } as { id: number } | null } }))
let auth: typeof identity
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))
import { useTradingPreferencesStore } from '../tradingPreferences'
beforeEach(() => {
  auth = reactive({ user: { id: 1 } })
  const values = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
  })
  setActivePinia(createPinia())
})
it('defaults to confirmation and saves the preference separately for each account', () => {
  const store = useTradingPreferencesStore()
  expect(store.confirmBorrow).toBe(true)
  expect(store.setConfirmBorrow(false)).toBe(true)
  auth.user = { id: 2 }
  expect(store.confirmBorrow).toBe(true)
  auth.user = { id: 1 }
  expect(store.confirmBorrow).toBe(false)
  setActivePinia(createPinia())
  expect(useTradingPreferencesStore().confirmBorrow).toBe(false)
})
it('keeps confirmation enabled when local storage cannot be read or written', () => {
  vi.stubGlobal('localStorage', {
    getItem: () => {
      throw new Error('denied')
    },
    setItem: () => {
      throw new Error('denied')
    },
  })
  const store = useTradingPreferencesStore()
  expect(store.confirmBorrow).toBe(true)
  expect(store.setConfirmBorrow(false)).toBe(false)
  expect(store.confirmBorrow).toBe(true)
})
