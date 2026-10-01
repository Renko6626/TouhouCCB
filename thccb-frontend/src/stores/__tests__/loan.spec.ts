import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/api/loan', () => ({ loanApi: { quota: vi.fn() } }))
import { loanApi, type LoanQuota } from '@/api/loan'
import { useLoanStore } from '@/stores/loan'

describe('loan quota refresh', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('clears stale spendable cash while refreshing and keeps it unavailable on failure', async () => {
    const store = useLoanStore()
    store.quota = {
      enabled: true, cash: '5500', available_cash: '5500', restricted_cash: '0',
      debt: '100', max_borrow: '900', net_worth: '5400', leverage_k: '10',
      daily_rate: '0.01', last_accrued_at: null,
    } satisfies LoanQuota
    let fail!: (reason: Error) => void
    vi.mocked(loanApi.quota).mockImplementationOnce(() => new Promise((_, reject) => { fail = reject }))
    const pending = store.refresh()
    // Another page may have opened a short since this snapshot was fetched.
    expect(store.quota).toBeNull()
    fail(new Error('network unavailable'))
    await pending
    expect(store.quota).toBeNull()
    expect(store.error).toBe('network unavailable')
    expect(store.loading).toBe(false)
  })
})
