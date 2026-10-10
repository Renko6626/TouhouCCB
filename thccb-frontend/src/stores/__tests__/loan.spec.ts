import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/api/index', () => ({ default: { get: vi.fn(), post: vi.fn() } }))
vi.stubGlobal('localStorage', { getItem: () => null })
import api from '@/api/index'
import { useAuthStore } from '@/stores/auth'
import { type LoanQuota } from '@/api/loan'
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
    vi.mocked(api.get).mockImplementationOnce(() => new Promise((_, reject) => { fail = reject }))
    const pending = store.refresh()
    // Another page may have opened a short since this snapshot was fetched.
    expect(store.quota).toBeNull()
    fail(new Error('network unavailable'))
    await pending
    expect(store.quota).toBeNull()
    expect(store.error).toBe('network unavailable')
    expect(store.loading).toBe(false)
  })

  it.each(['borrow', 'repay', 'repayAll'] as const)('returns the %s result when the single account refresh fails', async (action) => {
    const store = useLoanStore()
    const result = { cash: '100', debt: '20', max_borrow: null, effective: '10' }
    vi.mocked(api.post).mockResolvedValueOnce(result)
    vi.mocked(api.get).mockRejectedValueOnce(new Error('network unavailable'))
    expect(await (action === 'repayAll' ? store.repayAll() : store[action]('10'))).toEqual(result)
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(api.get).toHaveBeenCalledTimes(1)
    expect(store.quota).toBeNull()
    expect(store.error).toContain('操作已完成，账户信息刷新失败')
  })

  it('keeps the newest refresh when an older GET finishes last', async () => {
    const store = useLoanStore()
    let finish!: (value: LoanQuota) => void
    vi.mocked(api.get).mockImplementationOnce(() => new Promise<LoanQuota>(resolve => { finish = resolve }))
    const oldRefresh = store.refresh()
    const current = { cash: '200' } as LoanQuota
    vi.mocked(api.get).mockResolvedValueOnce(current)
    await store.refresh()
    finish({ cash: '100' } as LoanQuota)
    await oldRefresh
    expect(store.quota).toEqual(current)
    expect(store.loading).toBe(false)
  })

  it('clears an account snapshot and ignores its pending GET after logout', async () => {
    const auth = useAuthStore()
    const store = useLoanStore()
    let finish!: (value: LoanQuota) => void
    vi.mocked(api.get).mockImplementationOnce(() => new Promise<LoanQuota>(resolve => { finish = resolve }))
    auth.accessToken = 'account-a'
    store.quota = { cash: '500' } as LoanQuota
    auth.user = { id: 1, email: '', username: 'a', cash: 0, debt: 0, is_active: true, is_superuser: false, tos_accepted_at: null }
    expect(store.quota).toBeNull()
    const pending = store.refresh()
    auth.accessToken = null
    expect(store.quota).toBeNull()
    expect(store.loading).toBe(false)
    finish({ cash: '100' } as LoanQuota)
    await pending
    expect(store.quota).toBeNull()
    expect(store.error).toBeNull()
  })

  it('does not refresh the new account after an old account POST completes', async () => {
    const auth = useAuthStore()
    const store = useLoanStore()
    auth.accessToken = 'account-a'
    let finish!: (value: { cash: string; debt: string; max_borrow: string }) => void
    vi.mocked(api.post).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const pending = store.borrow('10')
    auth.user = { id: 2, email: '', username: 'b', cash: 0, debt: 0, is_active: true, is_superuser: false, tos_accepted_at: null }
    finish({ cash: '100', debt: '10', max_borrow: '50' })
    await pending
    expect(api.get).not.toHaveBeenCalled()
    expect(store.quota).toBeNull()
  })


  it('leaves a timed-out POST rejected without retrying or fetching a success snapshot', async () => {
    const store = useLoanStore()
    vi.mocked(api.post).mockRejectedValueOnce(new Error('timeout of 15000ms exceeded'))
    await expect(store.borrow('10')).rejects.toThrow('timeout')
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(api.get).not.toHaveBeenCalled()
  })

})
