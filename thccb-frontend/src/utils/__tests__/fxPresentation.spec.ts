import { describe, expect, it } from 'vitest'
import { fxAvailableCash, fxHoldingValue, fxBuyBlockReason, fxPairAllowsSide, fxSellAllocation, fxGoldPerForeign } from '../fxPresentation'
import { mapFxError } from '@/api/fx'

describe('FX 交易展示口径', () => {
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

  it('传统模式负债禁止买入，统一模式负债可进入服务端风控检查', () => {
    expect(fxBuyBlockReason({ cash: 100, debt: 10 }, '1')).toContain('未还借款')
    expect(fxBuyBlockReason({ cash: 100, available_cash: '100', debt: 10, unified_credit_enabled: true }, '1')).toBe('')
    expect(fxBuyBlockReason({ cash: 100, available_cash: '100', debt: 10, unified_credit_enabled: true, credit_frozen: true }, '1')).toContain('冻结')
    // 统一模式只对负债买入执行新增风险检查；无债现金买入不应误禁。
    expect(fxBuyBlockReason({ cash: 100, available_cash: '100', debt: 0, unified_credit_enabled: true, credit_frozen: true }, '1')).toBe('')
    expect(fxBuyBlockReason({ cash: 1, debt: 0, unified_credit_enabled: false }, '1.000001')).toContain('余额不足')
    expect(fxBuyBlockReason(null, '1')).toContain('账户')
  })

  it('missing unified available cash blocks spending while explicit legacy mode retains cash fallback', () => {
    const summary = { cash: 100, debt: 0, unified_credit_enabled: true }
    expect(fxAvailableCash(summary)).toBeNull()
    expect(fxAvailableCash({ ...summary, available_cash: null })).toBeNull()
    expect(fxBuyBlockReason(summary, '1')).not.toBe('')
    expect(fxBuyBlockReason({ ...summary, available_cash: null }, '1')).not.toBe('')
    expect(fxAvailableCash({ ...summary, available_cash: '0' })).toBe('0')
    expect(fxAvailableCash({ ...summary, unified_credit_enabled: false })).toBe(100)
    expect(fxAvailableCash({ cash: 100 })).toBeNull()
  })

  it('只减仓状态与后端买卖许可一致', () => {
    expect(fxPairAllowsSide({ status: 'paused', reduce_only: true }, 'sell')).toBe(true)
    expect(fxPairAllowsSide({ status: 'paused', reduce_only: true }, 'buy')).toBe(false)
    expect(fxPairAllowsSide({ status: 'paused' }, 'sell')).toBe(false)
    expect(fxPairAllowsSide({ status: 'closed', reduce_only: true }, 'sell')).toBe(false)
    expect(fxPairAllowsSide({ status: 'trading', reduce_only: true }, 'buy')).toBe(false)
  })

  it('卖出预览先抵债，现金增加不为负；非统一模式不自动还债', () => {
    expect(fxSellAllocation('10.000001', { debt: 3, unified_credit_enabled: true }))
      .toEqual({ repayment: '3.000000', cashIncrease: '7.000001' })
    expect(fxSellAllocation('2', { debt: 3, unified_credit_enabled: true }))
      .toEqual({ repayment: '2.000000', cashIncrease: '0.000000' })
    expect(fxSellAllocation('10', { debt: 3, unified_credit_enabled: false }))
      .toEqual({ repayment: '0.000000', cashIncrease: '10.000000' })
    expect(fxSellAllocation('10', null)).toBeNull()
  })
})
