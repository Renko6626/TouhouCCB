import { resolveCreditRiskStatus, resolveMarginBannerState } from '../creditRiskStatus'
import { describe, expect, it } from 'vitest'
import {
  fxAvailableCash,
  fxNetBorrowAction,
  fxHoldingValue,
  fxBuyBlockReason,
  fxPairAllowsSide,
  fxSellAllocation,
  fxGoldPerForeign,
  fxShortGoldPerForeign,
  fxMarginPercent,
  fxOrderAmountLabel,
  fxHomeHoldings,
  fxFundingPreview,
  fxShortRiskStatus,
  fxOrderPrincipal,
  fxOrderScale,
  fxSpotClosePreview,
  fxShortClosePreview,
  fxHoldingAveragePrice,
} from '../fxPresentation'
import { mapFxError } from '@/api/fx'
import type { AccountShortPosition } from '@/types/user'

describe('FX 交易展示口径', () => {
  it('持仓均价取剩余成本或开空所得除以当前数量，空头数量包含欠币利息', () => {
    expect(fxHoldingAveragePrice('500', '300')).toBe('1.666666666666')
    expect(fxHoldingAveragePrice('130', '60.1')).toBe('2.163061564059')
    // 部分回补分摊所得后，按当前剩余欠币更新参考价。
    expect(fxHoldingAveragePrice('65', '30.05')).toBe('2.163061564059')
    expect(fxHoldingAveragePrice('130', '65')).toBe('2.000000000000')
  })

  it('清仓、成本缺失或无效时不生成虚假的零价格横线', () => {
    expect(fxHoldingAveragePrice('130', '0')).toBeNull()
    expect(fxHoldingAveragePrice(null, '60')).toBeNull()
    expect(fxHoldingAveragePrice('130', null)).toBeNull()
    expect(fxHoldingAveragePrice('-1', '60')).toBeNull()
    expect(fxHoldingAveragePrice('0', '60')).toBeNull()
  })

  const closeWallet = { pair_id: 1, foreign_amount: '100', cost_basis: '1000' }
  const sellQuote = {
    pair_id: 1,
    side: 'sell' as const,
    input_amount: '100',
    output_amount: '1100',
  }
  const closeShort = { pair_id: 1, pending_short_debt: '101', proceeds_basis_gold: '1000' }
  const coverQuote = {
    pair_id: 1,
    action: 'cover' as const,
    cover_all: true,
    actual_foreign_amount: '101.1',
    input_amount: '1050',
  }

  it('全部卖出的盈亏按净所得减持仓成本，与还款和现金增加分开', () => {
    expect(fxSpotClosePreview(sellQuote, closeWallet)).toEqual({
      basis: '1000.000000',
      pnl: '100.000000',
      full: true,
    })
    expect(fxSellAllocation('1100', { debt: 500 })).toEqual({
      repayment: '500.000000',
      cashIncrease: '600.000000',
    })
  })

  it('部分卖出按数量分摊成本，大金额中间乘除不丢六位精度', () => {
    expect(
      fxSpotClosePreview({ ...sellQuote, input_amount: '25', output_amount: '240' }, closeWallet),
    ).toEqual({ basis: '250.000000', pnl: '-10.000000', full: false })
    expect(
      fxSpotClosePreview(
        { ...sellQuote, input_amount: '1', output_amount: '3333333333.333333' },
        { ...closeWallet, foreign_amount: '3', cost_basis: '9999999999.999999' },
      ),
    ).toEqual({ basis: '3333333333.333333', pnl: '0.000000', full: false })
  })

  it('全部回补使用完整历史所得，新计欠币利息包含在报价成本中', () => {
    expect(fxShortClosePreview(coverQuote, closeShort)).toEqual({
      basis: '1000.000000',
      pnl: '-50.000000',
      full: true,
    })
    expect(fxShortClosePreview(coverQuote, { ...closeShort, pending_short_debt: null })).toEqual({
      basis: '1000.000000',
      pnl: '-50.000000',
      full: true,
    })
  })

  it('部分回补按最近含息欠币分摊历史所得，保留收益基准', () => {
    expect(
      fxShortClosePreview(
        { ...coverQuote, cover_all: false, actual_foreign_amount: '50.5', input_amount: '450' },
        closeShort,
      ),
    ).toEqual({ basis: '500.000000', pnl: '50.000000', full: false })
  })

  it('无报价、错币种、未知成本或回补数量超出快照时不伪造盈亏', () => {
    expect(fxSpotClosePreview(null, closeWallet)).toBeNull()
    expect(fxSpotClosePreview(sellQuote, null)).toBeNull()
    expect(fxSpotClosePreview({ ...sellQuote, pair_id: 2 }, closeWallet)).toBeNull()
    expect(fxSpotClosePreview(sellQuote, { ...closeWallet, cost_basis: 'invalid' })).toBeNull()
    expect(fxShortClosePreview({ ...coverQuote, input_amount: null }, closeShort)).toBeNull()
    expect(fxShortClosePreview({ ...coverQuote, cover_all: false }, closeShort)).toBeNull()
    expect(
      fxShortClosePreview(
        { ...coverQuote, cover_all: false },
        { ...closeShort, pending_short_debt: null },
      ),
    ).toBeNull()
  })

  it('本金取扣债后的清算净值，未知或非正净值不能用于倍数开仓', () => {
    expect(fxOrderPrincipal({ liquidation_equity: 1000.000001 })).toBe('1000.000001')
    expect(fxOrderPrincipal(null)).toBeNull()
    expect(fxOrderPrincipal({ liquidation_equity: null })).toBeNull()
    expect(fxOrderPrincipal({ liquidation_equity: -1 })).toBeNull()
    expect(fxOrderPrincipal({ liquidation_equity: 0 })).toBeNull()
  })

  it('按本金倍数计算交易规模，保留六位精度并用账户现金自动拆借款', () => {
    expect(fxOrderScale('1000.000001', '0.25')).toBe('250.000000')
    expect(fxOrderScale('999999999.999999', '3.5')).toBe('3499999999.999996')
    const total = fxOrderScale('1000.000001', '2')!
    expect(fxFundingPreview(total, { cash: 400.000001, available_cash: '400.000001' })).toEqual({
      cashInput: '400.000001',
      shortfall: '1600.000001',
    })
    expect(fxOrderScale('100', '0')).toBe('0.000000')
    expect(fxOrderScale(null, '2')).toBeNull()
    expect(fxOrderScale('100', '-1')).toBeNull()
    expect(fxOrderScale('100', 'abc')).toBeNull()
  })
  it('额度拦截未进入风险模拟时不报告风险检查失败，真实失败仍保留', () => {
    const quote = {
      estimated_equity: null,
      estimated_risk_basis: null,
      margin_status: 'blocked' as const,
      risk_blocked_reason: null,
      blocked_reason: 'short_lending_limit',
    }
    expect(fxShortRiskStatus(quote, null)).toBe('unestimated')
    expect(
      fxShortRiskStatus({ ...quote, risk_blocked_reason: 'risk_engine_unavailable' }, null),
    ).toBe('blocked')
    expect(fxShortRiskStatus({ ...quote, blocked_reason: 'risk_engine_unavailable' }, null)).toBe(
      'blocked',
    )
  })
  const short: AccountShortPosition = {
    pair_id: 1,
    currency_code: 'MORA',
    principal_foreign: '100',
    interest_foreign: '1',
    pending_short_debt: '101',

    proceeds_basis_gold: '50.000001',
    reference_cover_cost: '40',
    reference_cover_fee: '0.1',
    executable: true,
    risk_status: 'ok',
    blocked_reason: null,
    interest_last_accrued_at: null,
  }

  it('首页包含纯空头，并以剩余开仓所得按收益基准汇总多空盈亏', () => {
    const shortOnly = fxHomeHoldings({
      fx_unrealized_pnl: 0,
      fx_cost_basis: 0,
      short_positions: [short],
    })
    expect(shortOnly.shortPositions).toEqual([short])
    expect(shortOnly.pnl).toBe('10.000001')
    expect(shortOnly.basis).toBe('50.000001')
    const mixed = fxHomeHoldings({
      fx_unrealized_pnl: -5,
      fx_cost_basis: 100,
      short_positions: [short],
    })
    expect(mixed.pnl).toBe('5.000001')
    expect(mixed.basis).toBe('150.000001')
  })

  it('任一空头无法回补估值时，首页汇总不能将缺失盈亏当成零', () => {
    const result = fxHomeHoldings({
      fx_unrealized_pnl: 5,
      fx_cost_basis: 100,
      short_positions: [short, { ...short, pair_id: 2, reference_cover_cost: null }],
    })
    expect(result.shortPositions).toHaveLength(2)
    expect(result.shortPnl).toBeNull()
    expect(result.pnl).toBeNull()
  })

  it('逐笔风控失败提示具体原因与恢复动作', () => {
    expect(mapFxError({ status: 400, data: { detail: 'insufficient_initial_margin' } })).toContain(
      '减少',
    )
    expect(mapFxError({ status: 400, data: { detail: 'credit_frozen' } })).toContain('冻结')
    expect(mapFxError({ status: 403, data: { detail: 'FX pair is reduce-only' } })).toContain(
      '减仓',
    )
  })
  it('只用当前钱包估值，保留六位资金精度，不依赖账户 FX 汇总', () => {
    expect(fxHoldingValue({ foreign_amount: '3.000001', cost_basis: '5.000001' }, '2')).toEqual({
      marketValue: '6.000002',
      pnl: '1.000001',
    })
    expect(
      fxHoldingValue({ foreign_amount: '9999999999.999999', cost_basis: '9999999999.999999' }, '1'),
    ).toEqual({ marketValue: '9999999999.999999', pnl: '0.000000' })
    expect(fxHoldingValue(null, '2')).toBeNull()
    expect(fxHoldingValue({ foreign_amount: '0', cost_basis: '0' }, '2')).toEqual({
      marketValue: '0.000000',
      pnl: '0.000000',
    })
  })

  it('买卖均价都显示金圆券 / 外币，而不取倒数精度受限的报价字段', () => {
    expect(fxGoldPerForeign({ side: 'buy', input_amount: '10', output_amount: '5' })).toBe(
      '2.000000000000',
    )
    expect(fxGoldPerForeign({ side: 'sell', input_amount: '5', output_amount: '10' })).toBe(
      '2.000000000000',
    )
  })

  it('借款买入、开空和回补均价包含输入费用，缺失成交数量时保留未知', () => {
    expect(fxOrderAmountLabel('1500.000000')).toBe('1,500')
    expect(fxOrderAmountLabel('0.000001')).toBe('0.000001')
    expect(fxGoldPerForeign({ side: 'buy', input_amount: '1500', output_amount: '600' })).toBe(
      '2.500000000000',
    )
    expect(
      fxShortGoldPerForeign({ action: 'open', input_amount: '100', output_amount: '198' }),
    ).toBe('1.980000000000')
    expect(
      fxShortGoldPerForeign({ action: 'cover', input_amount: '202', output_amount: '100' }),
    ).toBe('2.020000000000')
    expect(
      fxShortGoldPerForeign({ action: 'cover', input_amount: null, output_amount: null }),
    ).toBeNull()
    expect(fxGoldPerForeign({ side: 'buy', input_amount: '1500', output_amount: null })).toBeNull()
  })

  it('保证金率使用成交后的全账户净值和风险基数，零占用与估值缺失分开显示', () => {
    expect(fxMarginPercent('995', '1150')).toBe('86.52%')
    expect(fxMarginPercent('-10', '100')).toBe('-10.00%')
    expect(fxMarginPercent('995', '0')).toBe('无风险占用')
    expect(fxMarginPercent(null, '0')).toBe('—')
    expect(fxMarginPercent('995', null)).toBe('—')
  })

  it('负债买入进入服务端风控检查，冻结时阻止新增风险', () => {
    expect(fxBuyBlockReason({ cash: 100, available_cash: '100', debt: 10 }, '1')).toBe('')
    expect(
      fxBuyBlockReason({ cash: 100, available_cash: '100', debt: 10, credit_frozen: true }, '1'),
    ).toContain('冻结')
    // 冻结也可能由外币义务引起；金债为零不能绕过新增风险闸。
    expect(
      fxBuyBlockReason({ cash: 100, available_cash: '100', debt: 0, credit_frozen: true }, '1'),
    ).not.toBe('')
    expect(fxBuyBlockReason({ cash: 1, available_cash: '1', debt: 0 }, '1.000001')).toContain(
      '余额不足',
    )
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

  it('买入资金差额只使用账户现金，保留六位金额精度', () => {
    expect(
      fxFundingPreview('100.000001', {
        cash: 10.000001,
        available_cash: '10.000001',
      }),
    ).toEqual({ cashInput: '10.000001', shortfall: '90.000000' })
  })

  it('买入资金充足时只预览本笔投入，不把全部余额算作投入', () => {
    expect(
      fxFundingPreview('5', {
        cash: 100,
        available_cash: '100',
      }),
    ).toEqual({ cashInput: '5.000000', shortfall: '0.000000' })
  })

  it('账户现金未知或输入无效时不伪造资金拆分', () => {
    expect(fxFundingPreview('5', { cash: 100 })).toBeNull()
    expect(fxFundingPreview('-1', { cash: 100, available_cash: '100' })).toBeNull()
  })

  it('只减仓状态与后端买卖许可一致', () => {
    expect(fxPairAllowsSide({ status: 'paused', reduce_only: true }, 'sell')).toBe(true)
    expect(fxPairAllowsSide({ status: 'paused', reduce_only: true }, 'buy')).toBe(false)
    expect(fxPairAllowsSide({ status: 'paused' }, 'sell')).toBe(false)
    expect(fxPairAllowsSide({ status: 'closed', reduce_only: true }, 'sell')).toBe(false)
    expect(fxPairAllowsSide({ status: 'trading', reduce_only: true }, 'buy')).toBe(false)
  })

  it('卖出预览先抵债，现金增加不为负', () => {
    expect(fxSellAllocation('10.000001', { debt: 3 })).toEqual({
      repayment: '3.000000',
      cashIncrease: '7.000001',
    })
    expect(fxSellAllocation('2', { debt: 3 })).toEqual({
      repayment: '2.000000',
      cashIncrease: '0.000000',
    })
    expect(fxSellAllocation('10', null)).toBeNull()
  })
})

describe('账户风险等级展示', () => {
  const boundary = { ratio: 0.25, initial: 1 / 3.5, maintenance: 0.2 }
  it('统一模式使用完整组合的权威等级，不以展示比率重新判定', () => {
    expect(resolveCreditRiskStatus({ ...boundary, authoritativeStatus: 'healthy' })).toBe('healthy')
    expect(
      resolveCreditRiskStatus({ ...boundary, authoritativeStatus: 'blocked', noRisk: true }),
    ).toBe('blocked')
    expect(
      resolveCreditRiskStatus({ ...boundary, authoritativeStatus: 'unknown', noRisk: true }),
    ).toBe('unknown')
    expect(resolveCreditRiskStatus(boundary)).toBe('unknown')
  })
})

import { fxNetActionLabel } from '../fxPresentation'
it('describes net results from their real legs', () => {
  expect(
    fxNetActionLabel([
      { purpose: 'spot', side: 'sell' },
      { purpose: 'short_open', side: 'sell' },
    ]),
  ).toBe('平多后开空')
  expect(
    fxNetActionLabel([
      { purpose: 'short_cover', side: 'buy' },
      { purpose: 'borrow_buy', side: 'buy' },
    ]),
  ).toBe('回补后做多')
  expect(fxNetActionLabel([{ purpose: 'short_cover', side: 'buy' }])).toBe('回补空头')
})

import { fxGroupPersonalTrades } from '../fxPresentation'
import type { FxPersonalTrade } from '@/types/fx'
it('groups only linked legs and retains the real leg details', () => {
  const row = {
    id: 1,
    pair_id: 1,
    side: 'sell',
    purpose: 'spot',
    input_amount: '100',
    output_amount: '90',
    fee_amount: '1',
    post_price: '1',
    created_at: '2030-01-01',
    currency_code: 'T',
    currency_name: '测试',
    is_liquidation: false,
  } as FxPersonalTrade
  const groups = fxGroupPersonalTrades([
    {
      ...row,
      id: 3,
      execution_id: 5,
      execution_leg: 2,
      purpose: 'short_open',
      input_amount: '40',
      output_amount: '30',
    },
    { ...row, id: 2, execution_id: 5, execution_leg: 1 },
    row,
  ])
  expect(groups).toHaveLength(2)
  expect(groups[0]?.actionLabel).toBe('平多后开空')
  expect(groups[0]?.input_amount).toBe('140.000000')
  expect(groups[0]?.output_amount).toBe('120.000000')
  expect(groups[0]?.legs.map((leg) => leg.id)).toEqual([2, 3])
  expect(groups[1]?.legs).toHaveLength(1)
})

import { fxNetFundingPreview, fxNetAllowsGrowth, fxNetNewExposure } from '../fxPresentation'
it('net financing spends account cash and loans only the new long gap', () => {
  const summary = { cash: '150', available_cash: '150' }
  const cross = { legs: [
    { purpose: 'short_cover' as const, side: 'buy' as const, input_amount: '87.834871' },
    { purpose: 'spot' as const, side: 'buy' as const, input_amount: '112.165129' },
  ] }
  expect(fxNetFundingPreview('200', summary, cross)).toEqual({
    cashInput: '150.000000', shortfall: '50.000000', loanApplicable: true,
  })
  const pureCover = { legs: [{ purpose: 'short_cover' as const, side: 'buy' as const, input_amount: '80' }] }
  expect(fxNetFundingPreview('80', summary, pureCover)).toEqual({
    cashInput: '80.000000', shortfall: '0.000000', loanApplicable: false,
  })
  expect(fxNetFundingPreview('200', { cash: '50', available_cash: '50' }, cross)?.loanApplicable).toBe(false)
})

it('quick close never permits growth and return estimates use only the opening leg', () => {
  expect(fxNetAllowsGrowth({ growth_amount: '40' }, true)).toBe(false)
  expect(fxNetAllowsGrowth({ growth_amount: '40' }, false)).toBe(true)
  expect(fxNetAllowsGrowth({ growth_amount: '0' }, false)).toBe(false)
  const cover = {
    purpose: 'short_cover' as const,
    side: 'buy' as const,
    input_amount: '80',
    output_amount: '75',
  }
  expect(fxNetNewExposure([cover])).toBeNull()
  expect(
    fxNetNewExposure([
      cover,
      { purpose: 'spot', side: 'buy', input_amount: '20', output_amount: '15' },
    ]),
  ).toEqual({ direction: 'long', quantity: '15' })
  expect(
    fxNetNewExposure([
      { purpose: 'spot', side: 'sell', input_amount: '100', output_amount: '90' },
      { purpose: 'short_open', side: 'sell', input_amount: '40', output_amount: '30' },
    ]),
  ).toEqual({ direction: 'short', quantity: '40' })
})


it('selects authoritative home banners and fails closed only for active trading state', () => {
  const home = { trading: false, authenticated: true, ready: false, preview: null }
  expect(resolveMarginBannerState({ ...home, summary: { risk_status: 'healthy' } }).visible).toBe(false)
  expect(resolveMarginBannerState({ ...home, summary: { risk_status: 'danger' } })).toMatchObject({ status: 'danger', visible: true })
  expect(resolveMarginBannerState({ ...home, authenticated: false, summary: null }).visible).toBe(false)
  expect(resolveMarginBannerState({ ...home, summary: { risk_status: 'blocked', blocked_reason: 'missing asset' } })).toMatchObject({ status: 'blocked', blockedReason: 'missing asset' })
  expect(resolveMarginBannerState({ ...home, trading: true, summary: { risk_status: 'healthy' } })).toMatchObject({ status: 'blocked', visible: true })
  expect(resolveMarginBannerState({ ...home, trading: true, ready: true,
    preview: { risk_status: 'warning', initial_condition: 'rejected', maintenance_condition: 'rejected' },
    summary: { risk_status: 'healthy' } })).toMatchObject({ status: 'danger', visible: true })
})

it('requires borrowing confirmation for the new short leg when selling through an existing long', () => {
  expect(fxNetBorrowAction({ borrow_amount: '0', legs: [
    { purpose: 'spot', side: 'sell', input_amount: '10', output_amount: '20' },
    { purpose: 'short_open', side: 'sell', input_amount: '5', output_amount: '9.9' },
  ] })).toEqual({ kind: 'short', borrowAmount: '5.000000', quantity: '5.000000', saleProceeds: '9.900000' })
  expect(fxNetBorrowAction({ borrow_amount: '0', legs: [
    { purpose: 'spot', side: 'sell', input_amount: '10', output_amount: '20' },
  ] })).toBeNull()
  expect(fxNetBorrowAction({ borrow_amount: '4', legs: [
    { purpose: 'short_cover', side: 'buy', input_amount: '2', output_amount: '1' },
    { purpose: 'borrow_buy', side: 'buy', input_amount: '8', output_amount: '3.9' },
  ] })).toEqual({ kind: 'long', borrowAmount: '4', quantity: '3.900000', saleProceeds: null })
})
