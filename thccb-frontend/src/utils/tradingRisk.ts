import type { AccountBasis, LmsrPricingGroup, LmsrPosition, PricingGroup, PreviewCondition, TradingRules } from '@/types/tradingContext'
import { add, ceil, cmp, decimal, display, formatUnits, halfEven, FX_MAX, mul, rationalUnits, SCALE, sub, units, type Rational } from './fxDecimal'
import { quoteFx } from './fxSimulation'
const ZERO: Rational = { n: 0n, d: 1n }, ONE: Rational = { n: 1n, d: 1n }
export interface AccruedDebt { amount: string; approximate: boolean; uncertainty: string }
/** Integer-day compounding is rational. Fractional days retain a visible nonlinear uncertainty. */
export function accrueTradingDebt(amount: string, clock: string | null, rate: string, now: string | Date): AccruedDebt {
  const base = units(amount), r = decimal(rate)
  const seconds = clock ? (new Date(now).getTime() - Date.parse(clock)) / 1000 : 0
  if (!Number.isFinite(seconds) || r.n < 0n || base < 0n) throw new Error('invalid interest input')
  if (base === 0n || seconds <= 0 || r.n === 0n) return { amount: formatUnits(base), approximate: false, uncertainty: '0.000000' }
  if (seconds % 86400 === 0 && seconds / 86400 <= 366) {
    const days = BigInt(seconds / 86400)
    return { amount: formatUnits(halfEven(base * (r.d + r.n) ** days, r.d ** days)), approximate: false, uncertainty: '0.000000' }
  }
  const value = Number(amount) * Math.exp(Math.log1p(Number(rate)) * seconds / 86400)
  if (!Number.isFinite(value) || value > Number.MAX_SAFE_INTEGER / 1e6) throw new Error('interest outside supported numerical range')
  // Cross-runtime pow/log/exp and decimal→binary conversion are not certified. Include the
  // six-place rounding discontinuity plus magnitude-dependent representational error.
  const errorUnits = BigInt(Math.max(1, Math.ceil(value * Number.EPSILON * 16 * 1e6)))
  return { amount: value.toFixed(6), approximate: true, uncertainty: formatUnits(errorUnits) }
}
function numberDecimal(value: number): Rational {
  if (!Number.isFinite(value)) throw new Error('nonfinite valuation')
  // Scientific notation is expanded without discarding the floating computation's digits.
  const [mantissa, exp] = value.toString().split('e')
  const d = decimal(mantissa!)
  const exponent = Number(exp ?? 0)
  return exponent >= 0 ? { n: d.n * 10n ** BigInt(exponent), d: d.d } : { n: d.n, d: d.d * 10n ** BigInt(-exponent) }
}
function roundHalfUpFloat(value: number): bigint {
  const r = numberDecimal(value)
  if (r.n < 0n) throw new Error('negative liquidation proceeds')
  return (r.n * SCALE * 2n + r.d) / (2n * r.d)
}
function lmsrCost(q: number[], b: number): number {
  const max = Math.max(...q)
  return b * (Math.log(q.reduce((s, v) => s + Math.exp((v - max) / b), 0)) + max / b)
}
export interface LmsrLiquidationLeg { outcome_id: number; gross: string; fee: string; net: string }
export interface LmsrGroupValue { mtm: Rational; liquidation: Rational; legs: LmsrLiquidationLeg[]; liquidation_known: boolean; approximate: boolean }
/** Full q and sorted rolling outcomes reproduce the production cost-difference contract. */
export function valueLmsrGroup(group: LmsrPricingGroup, positions: LmsrPosition[], feeRate: string, now: string | Date): LmsrGroupValue {
  if (group.q_dec.length !== group.outcome_ids.length || !group.outcome_ids.length || new Set(group.outcome_ids).size !== group.outcome_ids.length) throw new Error('invalid LMSR group')
  const ordered = group.outcome_ids.map((id, i) => ({ id, q: Number(group.q_dec[i]) })).sort((a, b) => a.id - b.id)
  const q = ordered.map(v => v.q), b = Number(group.b), rate = decimal(feeRate)
  if (!(b > 0) || q.some(v => !Number.isFinite(v)) || rate.n < 0n || rate.n >= rate.d) throw new Error('invalid LMSR input')
  const max = Math.max(...q), exps = q.map(v => Math.exp((v - max) / b)), sum = exps.reduce((s, v) => s + v, 0)
  let mtm = ZERO, liquidation = ZERO, cost = lmsrCost(q, b), known = true
  const legs: LmsrLiquidationLeg[] = []
  const held = new Map<number, bigint>()
  for (const position of positions) {
    const amount = units(position.amount)
    if (amount < 0n) throw new Error('negative position')
    held.set(position.outcome_id, (held.get(position.outcome_id) ?? 0n) + amount)
    const idx = ordered.findIndex(v => v.id === position.outcome_id)
    if (idx < 0 && amount > 0n) throw new Error('unknown LMSR outcome')
    if (idx >= 0) mtm = add(mtm, decimal(display(mul(rationalUnits(amount), numberDecimal(exps[idx]! / sum)))))
  }
  const open = group.status === 'trading' && (!group.closes_at || Date.parse(group.closes_at) > new Date(now).getTime())
  if (group.closes_at && !Number.isFinite(Date.parse(group.closes_at))) throw new Error('invalid market cutoff')
  if (open) for (let i = 0; i < ordered.length; i++) {
    const amount = held.get(ordered[i]!.id) ?? 0n
    if (!amount) continue
    const after = [...q]; after[i] = after[i]! - Number(formatUnits(amount))
    const costAfter = lmsrCost(after, b), gross = roundHalfUpFloat(cost - costAfter)
    const fee = halfEven(gross * rate.n, rate.d)
    liquidation = add(liquidation, rationalUnits(gross - fee))
    legs.push({ outcome_id: ordered[i]!.id, gross: formatUnits(gross), fee: formatUnits(fee), net: formatUnits(gross - fee) })
    q[i] = after[i]!; cost = costAfter
    if (!Number.isFinite(cost)) known = false
  }
  // Large q cancellation reproduces production's observed result, but cannot certify
  // adjacent six-place outcomes across runtimes for an admission boundary.
  return { mtm, liquidation, legs, liquidation_known: known, approximate: true }
}
/** Full-q production cost difference and rounding for one hypothetical LMSR writer order. */
export function previewLmsrOrder(account: AccountBasis, groups: PricingGroup[], rules: TradingRules,
  marketId: number, outcomeId: number, side: 'buy' | 'sell', amount: string, now: string | Date,
  previous?: TradingValuationCache): { account_after: AccountBasis; account_preview: AccountPreview; gross: string; fee: string; net: string } {
  const group = groups.find(g => g.product === 'lmsr' && g.id === marketId)
  if (group?.product !== 'lmsr' || group.outcome_ids.length !== group.q_dec.length
    || !group.outcome_ids.length || new Set(group.outcome_ids).size !== group.outcome_ids.length) throw new Error('invalid LMSR group')
  const index = group.outcome_ids.indexOf(outcomeId), quantity = units(amount), b = Number(group.b)
  const q = group.q_dec.map(Number), rate = decimal(rules.lmsr_sell_fee_rate)
  if (index < 0 || quantity <= 0n || !(b > 0) || q.some(v => !Number.isFinite(v))
    || rate.n < 0n || rate.n >= rate.d) throw new Error('invalid LMSR order')
  const qAfter = group.q_dec.map((value, i) => i === index
    ? formatUnits(units(value) + (side === 'buy' ? quantity : -quantity)) : value)
  const beforeCost = lmsrCost(q, b), afterCost = lmsrCost(qAfter.map(Number), b)
  const gross = roundHalfUpFloat(side === 'buy' ? afterCost - beforeCost : beforeCost - afterCost)
  const fee = side === 'buy' ? 0n : halfEven(gross * rate.n, rate.d), net = gross - fee
  if (gross <= 0n || net < 0n) throw new Error('LMSR output rounds to zero')
  const positions = account.lmsr_positions.map(p => ({ ...p }))
  let held = positions.find(p => p.outcome_id === outcomeId)
  if (!held) { held = { market_id: marketId, outcome_id: outcomeId, amount: '0', cost_basis: '0' }; positions.push(held) }
  const before = units(held.amount), after = side === 'buy' ? before + quantity : before - quantity
  if (after < 0n) throw new Error('insufficient LMSR holding')
  held.amount = formatUnits(after)
  held.cost_basis = side === 'buy' ? formatUnits(units(held.cost_basis) + net)
    : formatUnits(before === 0n ? 0n : halfEven(units(held.cost_basis) * after, before))
  let cash = units(account.cash), debt = account.debt, clock = account.debt_last_accrued_at
  const accrued = accrueTradingDebt(account.debt, clock, rules.loan_daily_rate, now)
  if (side === 'buy') cash -= net
  else {
    const repaid = account.auto_repay !== false ? (units(accrued.amount) < net ? units(accrued.amount) : net) : 0n
    cash += net - repaid; debt = formatUnits(units(accrued.amount) - repaid)
    if (repaid > 0n || units(accrued.amount) > units(account.debt)) clock = debt === '0.000000' ? null : new Date(now).toISOString()
  }
  if (cash < 0n) throw new Error('insufficient cash')
  const accountAfter = { ...account, cash: formatUnits(cash), debt, debt_last_accrued_at: clock, lmsr_positions: positions }
  const pricingAfter = groups.map(g => g.product === 'lmsr' && g.id === marketId ? { ...group, q_dec: qAfter } : g)
  // Include cost-difference cancellation/quantization uncertainty even after selling the last holding.
  const magnitude = Math.max(Math.abs(beforeCost), Math.abs(afterCost), ...q.map(Math.abs))
  const tradeUncertainty = formatUnits(BigInt(Math.max(1, Math.ceil(magnitude * Number.EPSILON * 32 * 1e6))))
  const preview = valueTradingAccount(accountAfter, pricingAfter, rules, now, previous,
    { gold: side === 'sell' ? accrued.uncertainty : '0', shorts: {}, equity: tradeUncertainty })
  return { account_after: accountAfter, account_preview: preview, gross: formatUnits(gross), fee: formatUnits(fee), net: formatUnits(net) }
}

export interface TradingValuationCache {
  lmsr: Record<string, { key: string; value: LmsrGroupValue }>
  fx: Record<string, { key: string; mtm: Rational; liquidation: Rational }>
  shorts: Record<string, { key: string; marginal: Rational; input: string }>
}
export interface TradingInterestUncertainty { gold: string; shorts: Record<number, string>; equity?: string }
export interface AccountPreview {
  has_debt: boolean | null
  valuation_cache: TradingValuationCache
  display_equity: string | null; liquidation_equity: string | null; available_cash: string | null
  positive_assets: string | null; short_cover_cost: string | null; gold_debt: string | null
  E: Rational | null; W: Rational | null; risk_basis: string | null
  initial_condition: PreviewCondition; maintenance_condition: PreviewCondition
  risk_status: 'ok' | 'warning' | 'unknown'; error_status: 'exact' | 'approximate' | 'unknown'
  unknown_reasons: string[]; group_values: Record<string, { mtm: string | null; liquidation: string | null }>
}
export function valueTradingAccount(account: AccountBasis, groups: PricingGroup[], rules: TradingRules, now: string | Date, previous?: TradingValuationCache, inheritedUncertainty?: TradingInterestUncertainty): AccountPreview {
  const cache: TradingValuationCache = { lmsr: {}, fx: {}, shorts: {} }
  const reasons: string[] = [], groupValues: AccountPreview['group_values'] = {}
  let assets = ZERO, mtm = ZERO, cover = ZERO, marginal = ZERO, displayKnown = true, liquidationKnown = true, uncertainty = ZERO
  let approximate = false
  const find = (product: PricingGroup['product'], id: number) => groups.find(g => g.product === product && g.id === id)
  const fail = (reason: string, displayToo = true) => { reasons.push(reason); liquidationKnown = false; if (displayToo) displayKnown = false }
  // Obligation presence is independent of successful accrual. A failed valuation
  // must never turn a persisted debt into the debt-free fast path.
  const presence = (amounts: string[]): boolean | null => {
    try {
      const values = amounts.map(units)
      return values.some(v => v < 0n) ? null : values.some(v => v > 0n)
    } catch { return null }
  }
  const goldDebtPresent = presence([account.debt])
  const shortDebtPresent = presence(account.fx_shorts.flatMap(v => [v.principal_foreign, v.interest_foreign]))
  let goldDebtKnown = false
  let cash: Rational = ZERO, debt: Rational = ZERO, available = '0.000000'
  try {
    cash = decimal(account.cash)
    uncertainty = add(uncertainty, decimal(inheritedUncertainty?.equity ?? '0'))
    approximate ||= units(inheritedUncertainty?.equity ?? '0') > 0n
    if (account.fx_wallets.some(v => units(v.foreign_amount) < 0n) || account.fx_shorts.some(v => [v.principal_foreign, v.interest_foreign, v.restricted_gold, v.proceeds_basis_gold].some(amount => units(amount) < 0n))) throw new Error('invalid negative position')
    available = formatUnits(units(account.cash) - account.fx_shorts.reduce((s, v) => s + units(v.restricted_gold), 0n))
    if (units(available) < 0n) fail('restricted_exceeds_cash', false)
    const accrued = accrueTradingDebt(account.debt, account.debt_last_accrued_at, rules.loan_daily_rate, now)
    if (units(accrued.amount) > FX_MAX) throw new Error('gold debt storage overflow')
    debt = decimal(accrued.amount); goldDebtKnown = true; uncertainty = add(uncertainty, add(decimal(accrued.uncertainty), decimal(inheritedUncertainty?.gold ?? '0'))); approximate ||= accrued.approximate || units(inheritedUncertainty?.gold ?? '0') > 0n
    const markets = new Set(account.lmsr_positions.filter(v => units(v.amount) > 0n).map(v => v.market_id))
    for (const id of markets) {
      if (id === null) { fail('missing_market_identity'); continue }
      const g = find('lmsr', id)
      if (!g || g.product !== 'lmsr') { fail(`missing_lmsr:${id}`); continue }
      try {
        const positions = account.lmsr_positions.filter(v => v.market_id === id)
        const key = JSON.stringify([g, positions, rules.lmsr_sell_fee_rate, !g.closes_at || Date.parse(g.closes_at) > new Date(now).getTime()])
        const old = previous?.lmsr[id]
        const value = old?.key === key ? old.value : valueLmsrGroup(g, positions, rules.lmsr_sell_fee_rate, now)
        cache.lmsr[id] = { key, value }
        assets = add(assets, value.liquidation); mtm = add(mtm, value.mtm); approximate ||= value.approximate
        if (value.approximate) {
          const magnitude = Math.max(...g.q_dec.map(v => Math.abs(Number(v))))
          uncertainty = add(uncertainty, decimal((Math.max(1, Math.ceil(magnitude * Number.EPSILON * 16 * 1e6)) / 1e6).toFixed(6)))
        }
        if (!value.liquidation_known) fail(`unknown_lmsr:${id}`, false)
        groupValues[`lmsr:${id}`] = { mtm: display(value.mtm), liquidation: display(value.liquidation) }
      } catch { fail(`invalid_lmsr:${id}`) }
    }
    for (const wallet of account.fx_wallets) {
      if (units(wallet.foreign_amount) <= 0n) continue
      const g = find('fx', wallet.pair_id)
      if (!g || g.product !== 'fx') { fail(`missing_fx:${wallet.pair_id}`); continue }
      try {
        const gUnits = units(g.gold_reserve), fUnits = units(g.foreign_reserve)
        if (gUnits <= 0n || fUnits <= 0n) throw new Error('invalid reserve')
        const key = JSON.stringify([g, wallet.foreign_amount])
        const old = previous?.fx[g.id]
        const value = old?.key === key ? old.mtm : { n: units(wallet.foreign_amount) * gUnits, d: fUnits * SCALE }
        mtm = add(mtm, decimal(display(value)))
        let liquid = old?.key === key ? old.liquidation : ZERO
        if (old?.key !== key && (g.status === 'trading' || g.status === 'paused' && g.reduce_only)) {
          try { liquid = decimal(quoteFx('sell', wallet.foreign_amount, g).output_amount) } catch (e) {
            if (!(e instanceof Error) || !/fee leaves no input|output rounds to zero/.test(e.message)) throw e
          }
        }
        cache.fx[g.id] = { key, mtm: value, liquidation: liquid }
        assets = add(assets, liquid); groupValues[`fx:${g.id}`] = { mtm: display(value), liquidation: display(liquid) }
      } catch { fail(`invalid_fx:${wallet.pair_id}`) }
    }
    for (const short of account.fx_shorts) {
      const total = units(short.principal_foreign) + units(short.interest_foreign)
      if (!total) continue
      const g = find('fx', short.pair_id)
      if (!g || g.product !== 'fx') { fail(`missing_short:${short.pair_id}`); continue }
      try {
        const accrued = accrueTradingDebt(formatUnits(total), short.interest_last_accrued_at, rules.loan_daily_rate, now)
        const foreignError = units(accrued.uncertainty) + units(inheritedUncertainty?.shorts[short.pair_id] ?? '0')
        approximate ||= accrued.approximate || foreignError > 0n
        const gu = units(g.gold_reserve), fu = units(g.foreign_reserve)
        if (gu <= 0n || fu <= 0n) throw new Error('invalid reserve')
        const key = JSON.stringify([g, accrued.amount])
        const old = previous?.shorts[g.id]
        const marginalValue = old?.key === key ? old.marginal : decimal(display({ n: units(accrued.amount) * gu, d: fu * SCALE }))
        marginal = add(marginal, marginalValue)
        try {
          const input = old?.key === key ? old.input : quoteFx('buyExactOut', accrued.amount, g).input_amount
          cover = add(cover, decimal(input))
          cache.shorts[g.id] = { key, marginal: marginalValue, input }
          if (foreignError > 0n) {
            const debtError = foreignError
            const center = units(accrued.amount)
            try {
              const lower = center > debtError ? units(quoteFx('buyExactOut', formatUnits(center - debtError), g).input_amount) : 0n
              const upper = units(quoteFx('buyExactOut', formatUnits(center + debtError), g).input_amount)
              uncertainty = add(uncertainty, rationalUnits(upper - lower))
            } catch { fail(`short_interest_uncertainty:${g.id}`, false) }
          }
        } catch { fail(`short_not_fully_coverable:${g.id}`, false) }
      } catch { fail(`invalid_short:${short.pair_id}`) }
    }
  } catch { fail('invalid_account_input') }
  const E = liquidationKnown ? sub(sub(add(cash, assets), debt), cover) : null
  const displayEquity = displayKnown ? sub(sub(add(cash, mtm), debt), marginal) : null
  let W: Rational | null = null, riskBasis: string | null = null
  let initial: PreviewCondition = 'unknown', maintenance: PreviewCondition = 'unknown'
  const definitelyDebtFree = goldDebtPresent === false && shortDebtPresent === false && goldDebtKnown
  if (definitelyDebtFree) { initial = 'satisfied'; maintenance = 'satisfied' }
  else if (E && rules.leverage && rules.maintenance_ratio) {
    try {
      const L = decimal(rules.leverage), lm1 = sub(L, ONE), m = decimal(rules.maintenance_ratio)
      if (cmp(L, ONE) <= 0 || cmp(m, ZERO) <= 0) throw new Error('invalid thresholds')
      const ld = mul(L, debt), la = mul(lm1, assets)
      W = add(cmp(ld, la) >= 0 ? ld : la, mul(lm1, cover))
      riskBasis = formatUnits(ceil(W.n * L.d * SCALE, W.d * L.n))
      const initialDiff = sub(mul(mul(L, lm1), E), W)
      const maintenanceDiff = sub(mul(L, E), mul(m, W))
      // Propagate the identified nonlinear amount uncertainty through E and W.
      const initialError = mul(add(mul(L, lm1), L), uncertainty)
      const maintenanceError = mul(add(L, mul(m, L)), uncertainty)
      const decide = (diff: Rational, error: Rational): PreviewCondition => cmp(diff, error) >= 0 ? 'satisfied' : cmp(diff, { n: -error.n, d: error.d }) < 0 ? 'rejected' : 'unknown'
      initial = decide(initialDiff, initialError); maintenance = decide(maintenanceDiff, maintenanceError)
      if (initial === 'unknown' || maintenance === 'unknown') reasons.push('numerical_threshold_uncertainty')
    } catch { reasons.push('invalid_risk_rules') }
  }
  // Margin exemption does not establish valuation; either uncertain threshold stays visible.
  const riskStatus: AccountPreview['risk_status'] = !liquidationKnown || !displayKnown || initial === 'unknown' || maintenance === 'unknown'
    ? 'unknown' : maintenance === 'rejected' ? 'warning' : 'ok'
  return { has_debt: goldDebtPresent === true || shortDebtPresent === true ? true : goldDebtPresent === null || shortDebtPresent === null ? null : false, valuation_cache: cache, display_equity: displayEquity ? display(displayEquity) : null, liquidation_equity: E ? display(E) : null, available_cash: units(available) < 0n ? null : available, positive_assets: liquidationKnown ? display(assets) : null, short_cover_cost: liquidationKnown ? display(cover) : null, gold_debt: goldDebtKnown ? display(debt) : null, E, W, risk_basis: riskBasis, initial_condition: initial, maintenance_condition: maintenance, risk_status: riskStatus, error_status: !liquidationKnown || !displayKnown ? 'unknown' : approximate ? 'approximate' : 'exact', unknown_reasons: reasons, group_values: groupValues }
}
