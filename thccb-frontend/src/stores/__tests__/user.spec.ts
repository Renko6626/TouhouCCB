// 账户展示与风险快照使用服务端权威估值。
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true }),
}))

vi.mock('@/api/user', () => ({ userApi: { getSummary: vi.fn() } }))
vi.mock('@/api/market', () => ({ marketApi: { getMarkets: vi.fn().mockResolvedValue([]) } }))
import { userApi } from '@/api/user'

import { useUserStore } from '@/stores/user'
import type { UserSummary } from '@/types/user'

function makeSummary(overrides: Partial<UserSummary> = {}): UserSummary {
  return {
    cash: 100,
    debt: 0,
    fx_mtm: 0,
    fx_cost_basis: 0,
    fx_unrealized_pnl: 0,
    positions: [],
    sell_fee_rate: 0,
    rank_thresholds: [
      { min_net_worth: 120, title: 'Pro' },
      { min_net_worth: null, title: 'Rookie' },
    ],
    risk_status: 'healthy',
    last_liquidated_at: null,
    ...overrides,
  }
}

describe('unified credit snapshot', () => {
  beforeEach(() => setActivePinia(createPinia()))
  it('uses authoritative equity for rank and includes FX unrealized profit', () => {
    const store = useUserStore()
    store.summary = makeSummary({ cash: 150,
      debt: 50, debt_with_interest: 55, fx_mtm: 30, fx_cost_basis: 20, fx_unrealized_pnl: 10,
      display_equity: 125, liquidation_equity: 120, risk_basis: '55', equity_to_risk_basis: 120 / 55 })
    expect(store.netWorth).toBe(125)
    expect(store.netWorthLcv).toBe(120)
    expect(store.marginRatioEstimate).toBeCloseTo(120 / 55)
    expect(store.unrealizedPnl).toBe(10)
    expect(store.rankTitle).toBe('Pro')

    store.summary = makeSummary({ display_equity: 100, liquidation_equity: 100 })
    expect(store.netWorth).toBe(100)
    expect(store.unrealizedPnl).toBe(0)
    expect(store.rankTitle).toBe('Rookie')
  })
})

// Unknown liabilities must not turn locked short proceeds into wealth or a rank.
describe('unknown short valuation', () => {
  beforeEach(() => setActivePinia(createPinia()))
  it('keeps unknown equity and rank unavailable instead of ranking cash', () => {
    const store = useUserStore()
    store.summary = makeSummary({ cash: 5500,
      display_equity: null, liquidation_equity: null, risk_status: 'blocked' })
    expect(store.netWorth).toBeNull()
    expect(store.netWorthLcv).toBeNull()
    expect(store.rankTitle).toBe('估值待恢复')
    expect(store.marginRatioEstimate).toBeNull()
    store.summary.display_equity = 100
    expect(store.netWorth).toBe(100)
    expect(store.rankTitle).toBe('Rookie')
  })
})

// A failed post-fill refresh must not leave pre-buy spendable cash actionable.
describe('unified local fill refresh failure', () => {
  beforeEach(() => setActivePinia(createPinia()))
  it('blocks spending and equity until a fresh summary replaces the pre-fill snapshot', async () => {
    const store = useUserStore()
    store.summary = makeSummary({ cash: 5500,
      available_cash: '500', restricted_cash: '5000', display_equity: 500,
      liquidation_equity: 450, risk_basis: '4500', equity_to_risk_basis: 0.1,
      risk_status: 'healthy' })
    vi.mocked(userApi.getSummary).mockRejectedValueOnce(new Error('refresh unavailable'))
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      store.applyTradeFill({ side: 'buy', outcomeId: 1, marketId: 1, shares: 10,
        pay: 400, newCash: 5100, outcomeLabel: 'A', marketTitle: 'Market' })
      await store.fetchSummary(false)
      expect(store.summary?.cash).toBe(5100)
      expect(store.summary?.available_cash).toBeNull()
      expect(store.netWorth).toBeNull()
      expect(store.netWorthLcv).toBeNull()
      expect(store.marginRatioEstimate).toBeNull()
      expect(store.summary?.risk_status).toBe('blocked')
    } finally {
      log.mockRestore()
    }
  })

  it('invalidates a prior spendable balance when an external trade makes refresh fail', async () => {
    const store = useUserStore()
    store.summary = makeSummary({ cash: 100,
      available_cash: '100', display_equity: 100, liquidation_equity: 100,
      risk_status: 'healthy' })
    vi.mocked(userApi.getSummary).mockRejectedValueOnce(new Error('refresh unavailable'))
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      await store.fetchSummary(false)
      expect(store.summary?.available_cash).toBeNull()
      expect(store.netWorth).toBeNull()
      expect(store.summary?.risk_status).toBe('blocked')
    } finally {
      log.mockRestore()
    }
  })
})
