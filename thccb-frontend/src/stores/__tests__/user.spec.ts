// I6 行为测试：summary.fx_mtm 纳入展示净值 / 浮盈 / rank，
// 但 LCV margin 口径（借款抵押 / 强平）保持不含 FX。
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: false }),
}))

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
    margin_hard_threshold: 0.2,
    margin_soft_threshold: 0.5,
    sell_fee_rate: 0,
    rank_thresholds: [
      { min_net_worth: 120, title: 'Pro' },
      { min_net_worth: null, title: 'Rookie' },
    ],
    margin_status: 'healthy',
    liquidation_protected: false,
    last_liquidated_at: null,
    ...overrides,
  }
}

describe('I6 user store：FX MTM 进展示净值/rank，不进 LCV', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('netWorth/unrealizedPnl/rankTitle 含 fx_mtm 与 FX 浮盈', () => {
    const store = useUserStore()
    store.summary = makeSummary({ fx_mtm: 30, fx_cost_basis: 20, fx_unrealized_pnl: 10 })
    expect(store.netWorth).toBe(130)
    expect(store.unrealizedPnl).toBe(10)
    // rank 阈值只看展示净值，因此 FX 市值可以把用户推过 120 门槛。
    expect(store.rankTitle).toBe('Pro')
  })

  it('FX 为零时净值与 rank 不虚增', () => {
    const store = useUserStore()
    store.summary = makeSummary()
    expect(store.netWorth).toBe(100)
    expect(store.unrealizedPnl).toBe(0)
    expect(store.rankTitle).toBe('Rookie')
  })

  it('LCV 口径与 margin 估算不含 fx_mtm', () => {
    const store = useUserStore()
    store.summary = makeSummary({ debt: 50, fx_mtm: 100, fx_cost_basis: 40 })
    // 展示净值 = 100 - 50 + 0(LMSR) + 100(FX)
    expect(store.netWorth).toBe(150)
    // LCV = 100 - 50 + 0，不含 FX 市值
    expect(store.netWorthLcv).toBe(50)
    expect(store.marginRatioEstimate).toBe(1)
  })
})
