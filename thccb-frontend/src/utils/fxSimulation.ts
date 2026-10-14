import type { AccountBasis, FxPricingGroup, PricingGroup, TradingRules, PreviewCondition } from '@/types/tradingContext'
import { ceil, decimal, halfEven, FX_MAX, formatUnits, units } from './fxDecimal'
import { accrueTradingDebt, valueTradingAccount, type AccountPreview, type TradingValuationCache, type TradingInterestUncertainty } from './tradingRisk'
export interface FxMath {
  input_amount: string; output_amount: string; fee_amount: string; net_input: string
  post_gold_reserve: string; post_foreign_reserve: string
}
/** Fees ceil6, outputs floor6; exact output performs both production ceil stages. */
export function quoteFx(side: 'buy' | 'sell' | 'buyExactOut', amount: string, pricing: FxPricingGroup): FxMath {
  let x = units(amount); const g = units(pricing.gold_reserve), f = units(pricing.foreign_reserve)
  const r = decimal(side === 'sell' ? pricing.sell_fee_rate : pricing.buy_fee_rate)
  if (r.d > 100000000n) throw new Error('fee rate exceeds storage precision')
  if (x <= 0n || g <= 0n || f <= 0n || r.n < 0n || r.n >= r.d) throw new Error('invalid AMM input')
  let out: bigint
  if (side === 'buyExactOut') {
    if (x >= f || [x, g, f].some(v => v > FX_MAX)) throw new Error('output exceeds reserve or storage range')
    const net = ceil(g * x, f - x)
    out = x; x = ceil(net * r.d, r.d - r.n)
  } else out = 0n
  const fee = ceil(x * r.n, r.d), net = x - fee
  if (net <= 0n) throw new Error('fee leaves no input')
  if (side !== 'buyExactOut') out = side === 'sell' ? g * net / (f + net) : f * net / (g + net)
  const pg = side === 'sell' ? g - out : g + net
  const pf = side === 'sell' ? f + net : f - out
  if (out <= 0n || [x, fee, net, pg, pf].some(v => v < 0n || v > FX_MAX) || pg <= 0n || pf <= 0n) throw new Error('quote exceeds storage range or output rounds to zero')
  if (side === 'buyExactOut' && pg * 1000000n > FX_MAX * pf) throw new Error('trade exceeds storage limit')
  if (side === 'buyExactOut' && pg * pf < g * f) throw new Error('exact output invariant failure')
  return { input_amount: formatUnits(x), output_amount: formatUnits(out), fee_amount: formatUnits(fee), net_input: formatUnits(net), post_gold_reserve: formatUnits(pg), post_foreign_reserve: formatUnits(pf) }
}
export interface FxOrder { side: 'buy' | 'sell'; amount: string; borrow_amount?: string; allow_growth?: boolean }
export interface FxLeg extends FxMath { purpose: 'spot' | 'borrow_buy' | 'short_open' | 'short_cover'; side: 'buy' | 'sell' }
export interface FxOrderPreview {
  output_amount: string; fee_amount: string; wallet_after: string; foreign_debt_after: string
  cash_delta: string; gold_debt_delta: string; repaid_gold: string
  final_direction: 'long' | 'short' | 'flat'
  interest_uncertainty: TradingInterestUncertainty
  legs: FxLeg[]; account_after: AccountBasis; pricing_after: FxPricingGroup; growth_amount: string
  condition: PreviewCondition; rejected_reasons: string[]; unchecked_reasons: string[]
  error_status: 'exact' | 'approximate' | 'unknown'; account_preview: AccountPreview | null
}
export function simulateFxOrder(account: AccountBasis, pricing: FxPricingGroup, rules: TradingRules, order: FxOrder, now: string | Date): FxOrderPreview {
  return simulateFxOrderInternal(account, pricing, rules, order, now)
}
/** Legacy cover writer buys the requested foreign quantity, never an exact-input gold budget. */
export function simulateFxShortCover(account: AccountBasis, pricing: FxPricingGroup, rules: TradingRules,
  foreignAmount: string | null, now: string | Date): FxOrderPreview {
  const target = account.fx_shorts.find(row => row.pair_id === pricing.id)
  if (!target) throw new Error('no_outstanding_short')
  const owed = accrueTradingDebt(formatUnits(units(target.principal_foreign) + units(target.interest_foreign)),
    target.interest_last_accrued_at, rules.loan_daily_rate, now).amount
  if (units(owed) <= 0n) throw new Error('no_outstanding_short')
  const requested = foreignAmount ?? owed
  const cost = quoteFx('buyExactOut', requested, pricing).input_amount
  return simulateFxOrderInternal(account, pricing, rules, { side: 'buy', amount: cost, borrow_amount: '0', allow_growth: false }, now, requested)
}
function simulateFxOrderInternal(account: AccountBasis, pricing: FxPricingGroup, rules: TradingRules,
  order: FxOrder, now: string | Date, exactCoverForeign?: string): FxOrderPreview {
  const a: AccountBasis = { ...account, fx_wallets: account.fx_wallets.map(v => ({ ...v })), fx_shorts: account.fx_shorts.map(v => ({ ...v })) }
  let coverBoundary = false
  let p = { ...pricing }; const legs: FxLeg[] = [], rejected: string[] = []
  const result: FxOrderPreview = { output_amount: '0', fee_amount: '0', wallet_after: '0', foreign_debt_after: '0', cash_delta: '0', gold_debt_delta: '0', repaid_gold: '0', final_direction: 'flat', interest_uncertainty: { gold: '0', shorts: {} }, legs, account_after: a, pricing_after: p, growth_amount: '0.000000', condition: 'satisfied', rejected_reasons: rejected, unchecked_reasons: ['treasury_inventory', 'lending_cap', 'submission_version_and_price_protection'], error_status: 'exact', account_preview: null }
  const block = (reason: string) => { if (!rejected.includes(reason)) rejected.push(reason) }
  try {
    const goldAccrued = accrueTradingDebt(a.debt, a.debt_last_accrued_at, rules.loan_daily_rate, now)
    result.interest_uncertainty.gold = goldAccrued.uncertainty
    let cash = units(a.cash), debt = units(goldAccrued.amount)
    const wallet = a.fx_wallets.find(v => v.pair_id === p.id) ?? { pair_id: p.id, foreign_amount: '0', cost_basis: '0' }
    if (!a.fx_wallets.includes(wallet)) a.fx_wallets.push(wallet)
    let holdings = units(wallet.foreign_amount), basis = units(wallet.cost_basis)
    for (const row of a.fx_shorts) {
      const total = units(row.principal_foreign) + units(row.interest_foreign)
      const accrued = accrueTradingDebt(formatUnits(total), row.interest_last_accrued_at, rules.loan_daily_rate, now)
      row.interest_foreign = formatUnits(units(accrued.amount) - units(row.principal_foreign))
      if (accrued.approximate) result.error_status = 'approximate'
      result.interest_uncertainty.shorts[row.pair_id] = accrued.uncertainty
      if (units(accrued.amount) > total) row.interest_last_accrued_at = new Date(now).toISOString()
    }
    if (accrueTradingDebt(a.debt, a.debt_last_accrued_at, rules.loan_daily_rate, now).approximate) result.error_status = 'approximate'
    const target = a.fx_shorts.find(v => v.pair_id === p.id) ?? { pair_id: p.id, principal_foreign: '0', interest_foreign: '0', proceeds_basis_gold: '0', interest_last_accrued_at: null }
    if (!a.fx_shorts.includes(target)) a.fx_shorts.push(target)
    let principal = units(target.principal_foreign), interest = units(target.interest_foreign), owed = principal + interest
    let proceeds = units(target.proceeds_basis_gold)
    if (holdings > 0n && owed > 0n) throw new Error('same_pair_long_and_short')
    let budget = units(order.amount), growth = 0n; const borrow = units(order.borrow_amount ?? '0')
    if (budget <= 0n || borrow < 0n || budget > FX_MAX) throw new Error('invalid order amount')
    const leg = (purpose: FxLeg['purpose'], side: 'buy' | 'sell', q: FxMath) => {
      // Net order admission checks every leg's price. Asset liquidation retains
      // the ordinary production AMM quote without this order-only gate.
      if (units(q.post_gold_reserve) * 1000000n > FX_MAX * units(q.post_foreign_reserve)) block('trade exceeds storage limit')
      legs.push({ ...q, purpose, side }); p = { ...p, gold_reserve: q.post_gold_reserve, foreign_reserve: q.post_foreign_reserve }; result.pricing_after = p
    }
    if (order.side === 'sell') {
      if (borrow) block('financing requires a new long leg')
      const own = holdings < budget ? holdings : budget
      if (own) {
        const q = quoteFx('sell', formatUnits(own), p); leg('spot', 'sell', q)
        const out = units(q.output_amount), repaid = a.auto_repay !== false ? (debt < out ? debt : out) : 0n
        if (cash + out > FX_MAX) block('trade exceeds storage limit')
        result.repaid_gold = formatUnits(repaid)
        cash += out - repaid; debt -= repaid; basis = holdings ? halfEven(basis * (holdings - own), holdings) : 0n; holdings -= own
      }
      const residual = budget - own
      if (residual) {
        const q = quoteFx('sell', formatUnits(residual), p); leg('short_open', 'sell', q)
        growth = residual; principal += residual; owed += residual
        cash += units(q.output_amount); proceeds += units(q.output_amount)
      }
    } else {
      if (owed) {
        let exact: FxMath | null = null
        try { exact = quoteFx('buyExactOut', formatUnits(owed), p) } catch { /* partial cover remains possible */ }
        const q = exactCoverForeign != null ? quoteFx('buyExactOut', exactCoverForeign, p)
          : exact && budget >= units(exact.input_amount) ? exact : quoteFx('buy', formatUnits(budget), p)
        coverBoundary = (exactCoverForeign != null ? units(q.output_amount) === owed : !!exact && q === exact) && result.error_status === 'approximate'
        const out = units(q.output_amount), input = units(q.input_amount)
        if (out > owed) throw new Error('cover output exceeds foreign debt')
        if (cash < input) block('insufficient cash to cover')
        const paidInterest = out < interest ? out : interest
        interest -= paidInterest; principal -= out - paidInterest
        proceeds = out === owed ? 0n : proceeds - proceeds * out / owed
        owed -= out; cash -= input; budget -= input; leg('short_cover', 'buy', q)
      }
      if (budget) {
        const q = quoteFx('buy', formatUnits(budget), p); leg(borrow ? 'borrow_buy' : 'spot', 'buy', q)
        growth = units(q.output_amount)
        if (cash + borrow > FX_MAX) block('trade exceeds storage limit')
        if (borrow > budget) block('borrow_amount exceeds new long budget')
        if (cash < budget - borrow) block('insufficient cash')
        cash += borrow - budget; debt += borrow; holdings += growth; basis += budget
      } else if (borrow) block('financing requires a new long leg')
    }
    a.cash = formatUnits(cash); a.debt = formatUnits(debt); a.debt_last_accrued_at = debt === 0n ? null : borrow > 0n || units(goldAccrued.amount) > units(account.debt) ? new Date(now).toISOString() : account.debt_last_accrued_at
    wallet.foreign_amount = formatUnits(holdings); wallet.cost_basis = formatUnits(basis)
    Object.assign(target, { principal_foreign: formatUnits(principal), interest_foreign: formatUnits(interest), proceeds_basis_gold: formatUnits(proceeds), interest_last_accrued_at: principal + interest === 0n ? null : legs.some(v => v.purpose === 'short_open') ? new Date(now).toISOString() : target.interest_last_accrued_at })
    Object.assign(result, { growth_amount: formatUnits(growth), output_amount: formatUnits(legs.reduce((sum, v) => sum + units(v.output_amount), 0n)), fee_amount: formatUnits(legs.reduce((sum, v) => sum + units(v.fee_amount), 0n)), wallet_after: formatUnits(holdings), foreign_debt_after: formatUnits(owed), cash_delta: formatUnits(cash - units(account.cash)), gold_debt_delta: formatUnits(debt - units(account.debt)), final_direction: holdings > 0n ? 'long' : owed > 0n ? 'short' : 'flat' })
    if (!rules.fx_enabled) block('FX trading is disabled')
    if (!a.is_active) block('account_inactive')
    if (p.status !== 'trading' && !(p.status === 'paused' && p.reduce_only)) block('FX pair is not trading')
    if (p.archived && legs.some(l => l.purpose === 'short_cover')) block('FX pair is not coverable')
    if (legs.some(l => l.purpose !== 'short_cover')) {
      if (a.is_bot) block('bot accounts cannot trade FX')
      if (!a.tos_accepted_at) block('TOS acceptance required')
    }
    if (growth) {
      if (order.allow_growth === false) block('growth_requires_confirmation')
      if (p.reduce_only || p.status !== 'trading' || p.archived) block('FX pair is reduce-only')
      if (legs.some(l => l.purpose === 'short_open') && !rules.fx_short_enabled) block('FX short opening is disabled')
      if ((borrow || legs.some(l => l.purpose === 'short_open')) && !rules.loan_enabled) block('loan_disabled')
    }
    if ([cash, debt, holdings, basis, units(p.gold_reserve), units(p.foreign_reserve)].some(v => v < 0n || v > FX_MAX)) block('trade exceeds storage limit')
    result.account_preview = valueTradingAccount(a, [p], rules, now, undefined, result.interest_uncertainty)
    const owes = debt > 0n || a.fx_shorts.some(v => units(v.principal_foreign) + units(v.interest_foreign) > 0n)
    if (growth && owes) {
      if (a.credit_frozen) block('credit_frozen')
      if (rules.credit_new_risk_frozen) block('frozen_by_operator')
      if (!rules.unified_credit_enabled || !rules.risk_engine_ready) block('risk_engine_unavailable')
      if (result.account_preview.initial_condition === 'rejected') block('insufficient_initial_margin')
    }
    result.condition = rejected.length ? 'rejected' : coverBoundary || growth > 0n && owes && result.account_preview.initial_condition === 'unknown' ? 'unknown' : 'satisfied'
    if (result.condition === 'unknown') result.unchecked_reasons.push('interest_exact_cover_uncertainty')
  } catch (e) { block(e instanceof Error ? e.message : 'invalid input'); result.condition = 'rejected' }
  return result
}
/** Revalues the changed pool after all legs; unchanged groups retain their input snapshots. */
export function valueFxPostTrade(_base: AccountBasis, preview: FxOrderPreview, groups: PricingGroup[], rules: TradingRules, now: string | Date, previous?: TradingValuationCache): AccountPreview {
  return valueTradingAccount(preview.account_after, [...groups.filter(g => !(g.product === 'fx' && g.id === preview.pricing_after.id)), preview.pricing_after], rules, now, previous, preview.interest_uncertainty)
}
