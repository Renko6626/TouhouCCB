import { describe, expect, it } from 'vitest'
import { quoteFx, simulateFxOrder, simulateFxShortCover, valueFxPostTrade } from '../fxSimulation'
import { units } from '../fxDecimal'
import { valueTradingAccount } from '../tradingRisk'
import type { AccountBasis, FxPricingGroup, TradingRules } from '@/types/tradingContext'
import golden from './tradingPreview.golden.json'
export const now = '2026-01-01T00:00:00Z'
export const pricing: FxPricingGroup = { product: 'fx', id: 1, stamp: { epoch: 'test', revision: '1', input_hash: 'x' }, as_of: now, gold_reserve: '1000', foreign_reserve: '2000', buy_fee_rate: '.01234567'.replace(/^\./, '0.'), sell_fee_rate: '0.01234567', pool_version: '1', status: 'trading', archived: false, reduce_only: false }
export const rules: TradingRules = { rule_version: 'r', loan_daily_rate: '0', lmsr_sell_fee_rate: '0.01234567', fx_enabled: true, fx_short_enabled: true, loan_enabled: true, unified_credit_enabled: true, credit_new_risk_frozen: false, risk_engine_ready: true, leverage: '3', initial_ratio: '0.5', maintenance_ratio: '0.2' }
export const account: AccountBasis = { user_id: 1, economic_version: '9007199254740993', cash: '100', debt: '0', debt_last_accrued_at: null, credit_frozen: false, is_active: true, is_bot: false, tos_accepted_at: now, last_liquidated_at: null, lmsr_positions: [], fx_wallets: [], fx_shorts: [] }
const short = { pair_id: 1, principal_foreign: '1', interest_foreign: '0.1', interest_last_accrued_at: now, restricted_gold: '5', proceeds_basis_gold: '5' }
describe('production FX stage rounding and sequential net preview', () => {
  it('retains 8dp fees, both exact-output ceil stages, large amounts and dust rejection', () => {
    const p = { ...pricing, buy_fee_rate: '0.12345678' }
    for (const [side, amount, expected] of [['buy', '123.456789', golden.production.buy8dp], ['buyExactOut', '1.000001', golden.production.exact8dp]] as const) {
      const q = quoteFx(side, amount, p)
      for (const key of ['input_amount', 'fee_amount', 'net_input', 'output_amount', 'post_gold_reserve', 'post_foreign_reserve'] as const) expect(q[key]).toBe(expected[key])
    }
    const large = quoteFx('buyExactOut', '4999999999.999999', { ...p, gold_reserve: '1', foreign_reserve: '9999999999.999999', buy_fee_rate: '0.00000001' })
    expect(large.input_amount).toBe(golden.production.large_exact.input_amount)
    expect(large.output_amount).toBe(golden.production.large_exact.output_amount)
    expect(() => quoteFx('buy', '0.000001', p)).toThrow('fee leaves no input')
    expect(() => quoteFx('buy', '1.0000001', p)).toThrow('six-place')
  })
  it('matches independent production sell, full cover then buy and partial cover outputs', () => {
    for (const vector of golden.net) {
      const a = { ...account, cash: vector.cash, fx_wallets: [{ pair_id: 1, foreign_amount: vector.held, cost_basis: '4' }], fx_shorts: vector.principal === '0' ? [] : [{ ...short, principal_foreign: vector.principal, interest_foreign: vector.interest, restricted_gold: vector.lock }] }
      const p = simulateFxOrder(a, pricing, rules, { side: vector.side as 'buy' | 'sell', amount: vector.amount }, now)
      expect(p.condition).toBe('satisfied')
      expect(units(p.account_after.fx_wallets[0]!.foreign_amount)).toBe(units(vector.quote.wallet_after))
      expect(units(p.account_after.fx_shorts[0]!.principal_foreign) + units(p.account_after.fx_shorts[0]!.interest_foreign)).toBe(units(vector.principal === '0' ? '0' : vector.quote.foreign_debt_after))
      expect(p.legs.map(v => [units(v.input_amount), units(v.output_amount), units(v.fee_amount)])).toEqual(vector.quote.legs.map(v => [units(v.input_amount), units(v.output_amount), units(v.fee_amount)]))
    }
  })
  it('uses the first sell leg reserves for the short-opening leg and preserves other locks', () => {
    const a = { ...account, fx_wallets: [{ pair_id: 1, foreign_amount: '10', cost_basis: '4' }], fx_shorts: [{ ...short, pair_id: 2, restricted_gold: '90' }] }
    const preview = simulateFxOrder(a, pricing, rules, { side: 'sell', amount: '15' }, now)
    expect(preview.legs.map(v => v.purpose)).toEqual(['spot', 'short_open'])
    expect(preview.legs[0]!.output_amount).toBe('4.914004')
    expect(preview.legs[1]!.output_amount).toBe('2.438935')
    expect(preview.legs[1]!.fee_amount).toBe('0.061729')
    expect(preview.legs[1]!.post_gold_reserve).toBe('992.647061')
    expect(preview.legs[1]!.post_foreign_reserve).toBe('2014.814814')
    expect(preview.account_after.fx_shorts.find(v => v.pair_id === 2)!.restricted_gold).toBe('90')
    expect(preview.condition).toBe('unknown') // other debt group has no snapshot
    expect(a.cash).toBe('100')
  })
  it('does not spend other locks or borrow money on a cover leg', () => {
    const a = { ...account, cash: '5', fx_shorts: [{ ...short, restricted_gold: '0' }, { ...short, pair_id: 2, restricted_gold: '5' }] }
    expect(simulateFxOrder(a, pricing, rules, { side: 'buy', amount: '1', borrow_amount: '1' }, now).rejected_reasons).toContain('insufficient cash to cover')
    expect(simulateFxOrder({ ...account, fx_shorts: [short] }, pricing, rules, { side: 'buy', amount: '0.2', borrow_amount: '0.1' }, now).rejected_reasons).toContain('financing requires a new long leg')
  })
  it('rejects intermediate funded cash and every-leg post-price overflow despite in-range final balances', () => {
    const funded = simulateFxOrder({ ...account, cash: '9999999998.999999' }, pricing, rules, { side: 'buy', amount: '3', borrow_amount: '2' }, now)
    expect(funded.account_after.cash).toBe('9999999997.999999')
    expect(funded.account_after.debt).toBe('2.000000')
    expect(funded.condition).toBe('rejected')
    expect(funded.rejected_reasons).toContain('trade exceeds storage limit')
    const expensive = { ...pricing, gold_reserve: '1000000', foreign_reserve: '0.000001', sell_fee_rate: '0' }
    const sell = simulateFxOrder({ ...account, fx_wallets: [{ pair_id: 1, foreign_amount: '0.000001', cost_basis: '0' }] }, expensive, rules, { side: 'sell', amount: '0.000001' }, now)
    expect(sell.condition).toBe('rejected')
    expect(sell.rejected_reasons).toContain('trade exceeds storage limit')
    expect(quoteFx('sell', '0.000001', expensive).output_amount).toBe('500000.000000')
    const accountValue = valueTradingAccount({ ...account, cash: '0', fx_wallets: [{ pair_id: 1, foreign_amount: '0.000001', cost_basis: '0' }] }, [expensive], rules, now)
    expect(accountValue.display_equity).toBe('1000000.000000')
    expect(accountValue.liquidation_equity).toBe('500000.000000')
    expect(accountValue.unknown_reasons).toEqual([])
  })
  it('checks frozen growth, preserves pure reductions, and revalues changed reserves', () => {
    const a = { ...account, debt: '1', credit_frozen: true, fx_wallets: [{ pair_id: 1, foreign_amount: '10', cost_basis: '4' }] }
    expect(simulateFxOrder(a, pricing, rules, { side: 'buy', amount: '1' }, now).rejected_reasons).toContain('credit_frozen')
    const reduction = simulateFxOrder(a, pricing, rules, { side: 'sell', amount: '10' }, now)
    expect(reduction.condition).toBe('satisfied')
    const buy = simulateFxOrder(account, pricing, rules, { side: 'buy', amount: '10' }, now)
    const after = valueFxPostTrade(account, buy, [pricing], rules, now)
    expect(after.display_equity).toBe(valueTradingAccount(buy.account_after, [buy.pricing_after], rules, now).display_equity)
    expect(after.display_equity).not.toBe(valueTradingAccount(buy.account_after, [pricing], rules, now).display_equity)
    const accrued = simulateFxOrder({ ...account, cash: '157.321328', debt: '100', debt_last_accrued_at: now }, pricing, { ...rules, loan_daily_rate: '0.1' }, { side: 'buy', amount: '0.000002' }, '2026-01-01T12:00:00Z')
    expect(valueFxPostTrade(account, accrued, [pricing], { ...rules, loan_daily_rate: '0.1' }, '2026-01-01T12:00:00Z').initial_condition).toBe('unknown')
  })
})


it('simulates legacy partial cover exact foreign output and its own released lock', () => {
  const pool = { ...pricing, gold_reserve: '1', foreign_reserve: '1000000', buy_fee_rate: '0', sell_fee_rate: '0' }
  const a = { ...account, cash: '0.000004', credit_frozen: true, fx_shorts: [
    { ...short, principal_foreign: '1.4', interest_foreign: '0.1', restricted_gold: '0.000003', proceeds_basis_gold: '0.000003' },
    { ...short, pair_id: 2, principal_foreign: '1', interest_foreign: '0', restricted_gold: '0.000001', proceeds_basis_gold: '0.000001' },
  ] }
  const p = simulateFxShortCover(a, pool, rules, '1', now)
  expect(p.condition).toBe('satisfied')
  expect(p.legs[0]).toMatchObject({ purpose: 'short_cover', input_amount: '0.000002', output_amount: '1.000000', fee_amount: '0.000000',
    post_gold_reserve: '1.000002', post_foreign_reserve: '999999.000000' })
  expect(p.foreign_debt_after).toBe('0.500000')
  expect(p.account_after.fx_shorts[0]).toMatchObject({ principal_foreign: '0.500000', interest_foreign: '0.000000',
    restricted_gold: '0.000001', proceeds_basis_gold: '0.000001', interest_last_accrued_at: now })
  expect(p.account_after.fx_shorts[1]?.restricted_gold).toBe('0.000001')
  expect(p.account_after.cash).toBe('0.000002')
  expect(p.growth_amount).toBe('0.000000')
  const whole = valueFxPostTrade(a, p, [pool, { ...pricing, id: 2 }], rules, now)
  expect(whole.liquidation_equity).not.toBeNull()
  expect(whole.available_cash).toBe('0.000000')
  const all = simulateFxShortCover({ ...a, cash: '0.000005' }, pool, rules, null, now)
  expect(all.output_amount).toBe('1.500000')
  expect(all.foreign_debt_after).toBe('0.000000')
  expect(all.account_after.fx_shorts[0]?.restricted_gold).toBe('0.000000')
  expect(all.account_after.fx_shorts[0]?.interest_last_accrued_at).toBeNull()
  const feeCover = simulateFxShortCover({ ...account, fx_shorts: [{ ...short, principal_foreign: '2', interest_foreign: '0.1' }] },
    { ...pricing, buy_fee_rate: '0.12345678' }, rules, '1.000001', now)
  expect(feeCover.legs[0]?.input_amount).toBe(golden.production.exact8dp.input_amount)
  expect(feeCover.legs[0]?.fee_amount).toBe(golden.production.exact8dp.fee_amount)
  expect(feeCover.account_after.fx_shorts[0]?.principal_foreign).toBe('1.099999')
  expect(feeCover.account_after.fx_shorts[0]?.interest_foreign).toBe('0.000000')
})
