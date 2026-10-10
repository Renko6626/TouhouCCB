import { resolveCreditRiskStatus } from '../creditRiskStatus'
import { describe, expect, it } from 'vitest'
import { fxAvailableCash, fxHoldingValue, fxBuyBlockReason, fxPairAllowsSide, fxSellAllocation, fxGoldPerForeign, fxHomeHoldings } from '../fxPresentation'
import { mapFxError } from '@/api/fx'
import type { AccountShortPosition } from '@/types/user'

describe('FX 交易展示口径', () => {
  const short: AccountShortPosition = {
    pair_id: 1, currency_code: 'MORA', principal_foreign: '100', interest_foreign: '1',
    pending_short_debt: '101', restricted_gold: '10', proceeds_basis_gold: '50.000001',
    reference_cover_cost: '40', reference_cover_fee: '0.1', executable: true,
    risk_status: 'ok', blocked_reason: null, interest_last_accrued_at: null,
  }

  it('首页包含纯空头，并以剩余开仓所得而非锁定现金汇总多空盈亏', () => {
    const shortOnly = fxHomeHoldings({ fx_unrealized_pnl: 0, fx_cost_basis: 0, short_positions: [short] })
    expect(shortOnly.shortPositions).toEqual([short])
    expect(shortOnly.pnl).toBe('10.000001')
    expect(shortOnly.basis).toBe('50.000001')
    const mixed = fxHomeHoldings({ fx_unrealized_pnl: -5, fx_cost_basis: 100, short_positions: [short] })
    expect(mixed.pnl).toBe('5.000001')
    expect(mixed.basis).toBe('150.000001')
  })

  it('任一空头无法回补估值时，首页汇总不能将缺失盈亏当成零', () => {
    const result = fxHomeHoldings({ fx_unrealized_pnl: 5, fx_cost_basis: 100,
      short_positions: [short, { ...short, pair_id: 2, reference_cover_cost: null }] })
    expect(result.shortPositions).toHaveLength(2)
    expect(result.shortPnl).toBeNull()
    expect(result.pnl).toBeNull()
  })

  it('逐笔风控失败提示具体原因与恢复动作', () => {
    expect(mapFxError({ status: 400, data: { detail: 'insufficient_initial_margin' } })).toContain('减少')
    expect(mapFxError({ status: 400, data: { detail: 'credit_frozen' } })).toContain('冻结')
    expect(mapFxError({ status: 403, data: { detail: 'FX pair is reduce-only' } })).toContain('卖出')
  })
  it('只用当前钱包估值，保留六位资金精度，不依赖账户 FX 汇总', () => {
    expect(fxHoldingValue({ foreign_amount: '3.000001', cost_basis: '5.000001' }, '2'))
      .toEqual({ marketValue: '6.000002', pnl: '1.000001' })
    expect(fxHoldingValue({ foreign_amount: '9999999999.999999', cost_basis: '9999999999.999999' }, '1'))
      .toEqual({ marketValue: '9999999999.999999', pnl: '0.000000' })
    expect(fxHoldingValue(null, '2')).toBeNull()
    expect(fxHoldingValue({ foreign_amount: '0', cost_basis: '0' }, '2'))
      .toEqual({ marketValue: '0.000000', pnl: '0.000000' })
  })

  it('买卖均价都显示金圆券 / 外币，而不取倒数精度受限的报价字段', () => {
    expect(fxGoldPerForeign({ side: 'buy', input_amount: '10', output_amount: '5' })).toBe('2.000000000000')
    expect(fxGoldPerForeign({ side: 'sell', input_amount: '5', output_amount: '10' })).toBe('2.000000000000')
  })

  it('负债买入进入服务端风控检查，冻结时阻止新增风险', () => {
    expect(fxBuyBlockReason({ cash: 100, available_cash: '100', debt: 10 }, '1')).toBe('')
    expect(fxBuyBlockReason({ cash: 100, available_cash: '100', debt: 10, credit_frozen: true }, '1')).toContain('冻结')
    // 冻结也可能由外币义务引起；金债为零不能绕过新增风险闸。
    expect(fxBuyBlockReason({ cash: 100, available_cash: '100', debt: 0, credit_frozen: true }, '1')).not.toBe('')
    expect(fxBuyBlockReason({ cash: 1, available_cash: '1', debt: 0 }, '1.000001')).toContain('余额不足')
    expect(fxBuyBlockReason(null, '1')).toContain('账户')
  })

  it('missing available cash blocks spending', () => {
    const summary = { cash: 100, debt: 0 }
    expect(fxAvailableCash(summary)).toBeNull()
    expect(fxAvailableCash({ ...summary, available_cash: null })).toBeNull()
    expect(fxBuyBlockReason(summary, '1')).not.toBe('')
    expect(fxBuyBlockReason({ ...summary, available_cash: null }, '1')).not.toBe('')
    expect(fxAvailableCash({ ...summary, available_cash: '0' })).toBe('0')
    expect(fxAvailableCash({ cash: 100 })).toBeNull()
  })

  it('只减仓状态与后端买卖许可一致', () => {
    expect(fxPairAllowsSide({ status: 'paused', reduce_only: true }, 'sell')).toBe(true)
    expect(fxPairAllowsSide({ status: 'paused', reduce_only: true }, 'buy')).toBe(false)
    expect(fxPairAllowsSide({ status: 'paused' }, 'sell')).toBe(false)
    expect(fxPairAllowsSide({ status: 'closed', reduce_only: true }, 'sell')).toBe(false)
    expect(fxPairAllowsSide({ status: 'trading', reduce_only: true }, 'buy')).toBe(false)
  })

  it('卖出预览先抵债，现金增加不为负', () => {
    expect(fxSellAllocation('10.000001', { debt: 3 }))
      .toEqual({ repayment: '3.000000', cashIncrease: '7.000001' })
    expect(fxSellAllocation('2', { debt: 3 }))
      .toEqual({ repayment: '2.000000', cashIncrease: '0.000000' })
    expect(fxSellAllocation('10', null)).toBeNull()
  })
})

describe('账户风险等级展示', () => {
  const boundary = { ratio: 0.25, initial: 1 / 3.5, maintenance: 0.2 }
  it('统一模式使用完整组合的权威等级，不以展示比率重新判定', () => {
    expect(resolveCreditRiskStatus({ ...boundary, authoritativeStatus: 'healthy' })).toBe('healthy')
    expect(resolveCreditRiskStatus({ ...boundary, authoritativeStatus: 'blocked', noRisk: true })).toBe('blocked')
    expect(resolveCreditRiskStatus({ ...boundary, authoritativeStatus: 'unknown', noRisk: true })).toBe('unknown')
    expect(resolveCreditRiskStatus(boundary)).toBe('unknown')
  })
})
