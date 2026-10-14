import { describe, expect, it } from 'vitest'
import { accrueTradingDebt, valueLmsrGroup, valueTradingAccount, previewLmsrOrder } from '../tradingRisk'
import { display } from '../fxDecimal'
import { resolveMarginBannerState } from '../creditRiskStatus'
import type { AccountBasis, FxPricingGroup, LmsrPricingGroup, TradingRules } from '@/types/tradingContext'
import golden from './tradingPreview.golden.json'
const now = '2026-01-01T00:00:00Z'
const rules: TradingRules = { rule_version: 'r', loan_daily_rate: '0', lmsr_sell_fee_rate: '0.01234567', fx_enabled: true, fx_short_enabled: true, loan_enabled: true, unified_credit_enabled: true, credit_new_risk_frozen: false, risk_engine_ready: true, leverage: '3', initial_ratio: '0.5', maintenance_ratio: '0.2' }
const account: AccountBasis = { user_id: 1, economic_version: '1', cash: '100', debt: '0', debt_last_accrued_at: null, credit_frozen: false, is_active: true, is_bot: false, tos_accepted_at: now, last_liquidated_at: null, lmsr_positions: [], fx_wallets: [], fx_shorts: [] }
const fx: FxPricingGroup = { product: 'fx', id: 1, stamp: { epoch: 'test', revision: '1', input_hash: 'x' }, as_of: now, gold_reserve: '1000', foreign_reserve: '2000', buy_fee_rate: '0.01234567', sell_fee_rate: '0.01234567', pool_version: '1', status: 'trading', archived: false, reduce_only: false }
function lmsr(q: string[], b: number): LmsrPricingGroup { return { product: 'lmsr', id: 1, stamp: fx.stamp, as_of: now, q_dec: q, outcome_ids: q.map((_, i) => i + 1), b: String(b), status: 'trading', closes_at: null } }
describe('whole-account liquidation and distinct display valuation', () => {
  it('matches production sorted full-q legs including saturated and large-q cases', () => {
    for (const vector of [golden.production.lmsr_normal, golden.production.lmsr_extreme, golden.production.lmsr_large_q]) {
      const positions = Object.entries(vector.held).map(([id, amount]) => ({ market_id: 1, outcome_id: Number(id), amount, cost_basis: '0' }))
      const value = valueLmsrGroup(lmsr(vector.q, vector.b), positions.reverse(), rules.lmsr_sell_fee_rate, now)
      expect(display(value.liquidation)).toBe(vector.net)
      expect(value.legs.map(v => [v.outcome_id, v.gross, v.fee, v.net])).toEqual(vector.legs.map(v => [Number(v.outcome_id), v.gross, v.fee, v.net]))
    }
  })
  it('includes paused MTM, uses zero halted liquidation assets and unknown full short cover separately', () => {
    const a = { ...account, fx_wallets: [{ pair_id: 1, foreign_amount: '10', cost_basis: '0' }] }
    const value = valueTradingAccount(a, [{ ...fx, status: 'paused' }], rules, now)
    expect(value.display_equity).toBe('105.000000')
    expect(value.liquidation_equity).toBe('100.000000')
    const short = { pair_id: 1, principal_foreign: '2000', interest_foreign: '0', interest_last_accrued_at: now, proceeds_basis_gold: '10' }
    const impossible = valueTradingAccount({ ...account, fx_shorts: [short] }, [fx], rules, now)
    expect(impossible.display_equity).toBe('-900.000000')
    expect(impossible.liquidation_equity).toBeNull()
    expect(impossible.short_cover_cost).toBeNull()
    expect(impossible.available_cash).toBe('100.000000')
    expect(impossible.risk_status).toBe('unknown')
    expect(valueTradingAccount({ ...account, fx_shorts: [short] }, [], rules, now).display_equity).toBeNull()
  })
  it('reuses immutable group inputs without retaining stale cutoff, holdings or interest values', () => {
    const market = { ...lmsr(['100', '200'], 100), closes_at: '2026-01-01T01:00:00Z' }
    const a = { ...account, lmsr_positions: [{ market_id: 1, outcome_id: 1, amount: '10', cost_basis: '0' }], fx_shorts: [{ pair_id: 1, principal_foreign: '1', interest_foreign: '0', interest_last_accrued_at: now, proceeds_basis_gold: '1' }] }
    const activeRules = { ...rules, loan_daily_rate: '0.1' }
    const first = valueTradingAccount(a, [market, fx], activeRules, now)
    const updated = { ...a, lmsr_positions: [{ ...a.lmsr_positions[0]!, amount: '20' }] }
    const later = '2026-01-02T00:00:00Z'
    const cached = valueTradingAccount(updated, [market, fx], activeRules, later, first.valuation_cache)
    const fresh = valueTradingAccount(updated, [market, fx], activeRules, later)
    expect(cached.liquidation_equity).toBe(fresh.liquidation_equity)
    expect(cached.display_equity).toBe(fresh.display_equity)
    expect(cached.short_cover_cost).toBe(fresh.short_cover_cost)
    expect(cached.short_cover_cost).not.toBe(first.short_cover_cost)
    expect(cached.group_values['lmsr:1']!.liquidation).toBe('0.000000')
  })
  it('retains a broken nullable market position and marks entire account unknown', () => {
    const a = { ...account, lmsr_positions: [{ market_id: null, outcome_id: 99, amount: '1', cost_basis: '1' }] }
    const result = valueTradingAccount(a, [], rules, now)
    expect(result.liquidation_equity).toBeNull()
    expect(result.unknown_reasons).toContain('missing_market_identity')
    expect(result.initial_condition).toBe('satisfied')
    expect(result.maintenance_condition).toBe('satisfied')
    expect(result.risk_status).toBe('unknown')
    expect(resolveMarginBannerState({ trading: true, authenticated: true, ready: true, preview: result, summary: null }))
      .toMatchObject({ status: 'blocked', visible: true })
    expect(a.lmsr_positions).toHaveLength(1)
  })
  it('uses unrounded rational boundaries and never applies initial margin to debt-free accounts', () => {
    // Hand calculation: D=2, L=3, W=6; initial E=1, maintenance E=.4.
    const initial = valueTradingAccount({ ...account, cash: '3', debt: '2' }, [], rules, now)
    expect(initial.initial_condition).toBe('satisfied')
    expect(valueTradingAccount({ ...account, cash: '2.999999', debt: '2' }, [], rules, now).initial_condition).toBe('rejected')
    expect(valueTradingAccount({ ...account, cash: '2.4', debt: '2' }, [], rules, now).maintenance_condition).toBe('satisfied')
    expect(valueTradingAccount({ ...account, cash: '2.399999', debt: '2' }, [], rules, now).maintenance_condition).toBe('rejected')
    expect(valueTradingAccount({ ...account, cash: '0' }, [], { ...rules, leverage: null, maintenance_ratio: null }, now))
      .toMatchObject({ initial_condition: 'satisfied', maintenance_condition: 'satisfied', risk_status: 'ok' })
  })
  it('never turns failed debt accrual or malformed obligation inputs into a debt-free healthy result', () => {
    const overflow = valueTradingAccount({ ...account, debt: '9999999999.999999', debt_last_accrued_at: now }, [], { ...rules, loan_daily_rate: '0.1' }, '2026-01-01T00:00:01Z')
    expect(overflow.has_debt).toBe(true)
    expect(overflow.gold_debt).toBeNull()
    expect(overflow.liquidation_equity).toBeNull()
    expect(overflow.initial_condition).toBe('unknown')
    expect(overflow.maintenance_condition).toBe('unknown')
    expect(overflow.risk_status).toBe('unknown')
    for (const invalid of [{ ...account, debt: 'invalid' }, { ...account, debt: '1', cash: 'invalid' }]) {
      const result = valueTradingAccount(invalid, [], rules, now)
      expect(result.gold_debt).toBeNull()
      expect(result.initial_condition).toBe('unknown')
      expect(result.maintenance_condition).toBe('unknown')
      expect(result.risk_status).toBe('unknown')
    }
  })
  it('rounds integer-day interest half-even, keeps tiny clocks stable and identifies uncertain fractional-day margins', () => {
    expect(accrueTradingDebt('0.000001', now, '0.1', '2026-01-02T00:00:00Z').amount).toBe(golden.production['interest_0.000001'].result)
    const halfDay = accrueTradingDebt('100', now, '0.1', '2026-01-01T12:00:00Z')
    expect(halfDay.amount).toBe(golden.production.interest_100.result)
    expect(halfDay.approximate).toBe(true)
    // Existing production discrepancy must stay explicit, never be hidden by a tolerance.
    const large = accrueTradingDebt('1000000000', now, '0.1', '2026-01-01T00:00:01Z')
    expect(large.approximate).toBe(true)
    expect(large.uncertainty).not.toBe('0.000000')
    const near = valueTradingAccount({ ...account, cash: '157321327.500000', debt: '100', debt_last_accrued_at: now }, [], { ...rules, loan_daily_rate: '0.1' }, '2026-01-01T12:00:00Z')
    expect(near.initial_condition).toBe('satisfied')
    const boundary = valueTradingAccount({ ...account, cash: '157.321328', debt: '100', debt_last_accrued_at: now }, [], { ...rules, loan_daily_rate: '0.1' }, '2026-01-01T12:00:00Z')
    expect(boundary.initial_condition).toBe('unknown')
    expect(boundary.maintenance_condition).toBe('satisfied')
    expect(boundary.risk_status).toBe('unknown')
    expect(resolveMarginBannerState({ trading: true, authenticated: true, ready: true, preview: boundary, summary: null }))
      .toMatchObject({ status: 'blocked', visible: true })
  })
})


it('uses complete-q writer proceeds for saturated hypothetical sale cash and debt', () => {
  const group = lmsr(['1000', '0'], 1), noFee = { ...rules, lmsr_sell_fee_rate: '0' }
  const a = { ...account, cash: '0', debt: '100', lmsr_positions: [{ market_id: 1, outcome_id: 1, amount: '1000', cost_basis: '10' }] }
  const result = previewLmsrOrder(a, [group], noFee, 1, 1, 'sell', '1000', now)
  expect(result.gross).toBe('999.306853') // independently 1000 - ln(2), HALF_UP6
  expect(result.fee).toBe('0.000000')
  expect(result.account_after.cash).toBe('899.306853')
  expect(result.account_after.debt).toBe('0.000000')
  expect(result.account_after.lmsr_positions[0]?.amount).toBe('0.000000')
  expect(result.account_preview.display_equity).toBe('899.306853')
  expect(result.account_preview.liquidation_equity).toBe('899.306853')
  expect(result.account_preview.error_status).toBe('approximate')
  expect(a.debt).toBe('100')
  const withFee = previewLmsrOrder({ ...a, debt: '0' }, [group], { ...noFee, lmsr_sell_fee_rate: '0.01' }, 1, 1, 'sell', '1000', now)
  expect(withFee.fee).toBe('9.993069')
  expect(withFee.account_after.cash).toBe('989.313784')
  // Marginal p underflows to zero; full-q buy risk must remain calculable.
  const buy = previewLmsrOrder({ ...account, cash: '2000' }, [group], noFee, 1, 2, 'buy', '1000', now)
  expect(buy.gross).toBe('0.693147')
  expect(buy.account_after.cash).toBe('1999.306853')
})

it('keeps LMSR sale proceeds in cash without repaying debt when the account setting is disabled', () => {
  const group = lmsr(['1000', '0'], 1)
  const a = { ...account, auto_repay: false, cash: '0', debt: '100', lmsr_positions: [{ market_id: 1, outcome_id: 1, amount: '1000', cost_basis: '10' }] }
  const sale = previewLmsrOrder(a, [group], { ...rules, lmsr_sell_fee_rate: '0' }, 1, 1, 'sell', '1000', now)
  expect(sale.gross).toBe('999.306853')
  expect(sale.account_after.cash).toBe('999.306853')
  expect(sale.account_after.debt).toBe('100.000000')
  expect(sale.account_preview.display_equity).toBe('899.306853')
  expect(sale.account_preview.liquidation_equity).toBe('899.306853')
})
