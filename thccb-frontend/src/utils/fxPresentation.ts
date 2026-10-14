import {
  addFxAmounts,
  allocateFxAmount,
  compareFxAmounts,
  divideFxAmount,
  formatFxAmount,
  multiplyFxAmount,
  subtractFxAmounts,
} from '@/api/fx'
import type {
  FxPairPublic,
  FxPersonalTrade,
  FxQuote,
  FxShortPosition,
  FxShortQuote,
  FxSide,
  FxWalletPublic,
} from '@/types/fx'
import type { UserSummary } from '@/types/user'
import { resolveCreditRiskStatus, type CreditRiskStatus } from './creditRiskStatus'

/** 外币额度/库存门槛先于风险模拟；没有成交后估值不等于风险引擎故障。 */
export function fxShortRiskStatus(
  quote: Pick<
    FxShortQuote,
    | 'estimated_equity'
    | 'estimated_risk_basis'
    | 'margin_status'
    | 'risk_blocked_reason'
    | 'blocked_reason'
  >,
  ratio: number | null,
): CreditRiskStatus | 'unestimated' {
  if (
    (quote.blocked_reason === 'short_lending_limit' ||
      quote.blocked_reason === 'insufficient_treasury_foreign') &&
    !quote.risk_blocked_reason &&
    quote.estimated_equity == null &&
    quote.estimated_risk_basis == null
  ) {
    return 'unestimated'
  }
  return resolveCreditRiskStatus({
    authoritativeStatus: quote.margin_status,
    ratio,
    noRisk: compareFxAmounts(quote.estimated_risk_basis, '0') === 0,
  })
}

type CreditSummary = Pick<UserSummary, 'debt' | 'debt_with_interest'>
type BuySummary = CreditSummary & Pick<UserSummary, 'cash' | 'credit_frozen' | 'available_cash'>

/** 用已有清算净值锚定本笔规模；缺失或非正本金不生成可拖动的倍数。 */
export function fxOrderPrincipal(summary: Pick<UserSummary, 'liquidation_equity'> | null) {
  const equity = summary?.liquidation_equity
  return compareFxAmounts(equity, '0') === 1 ? subtractFxAmounts(equity!, '0') : null
}

/** 倍数只确定本笔金圆券规模，不能代替全账户逐笔风险准入。 */
export function fxOrderScale(principal: string | null, multiplier: string) {
  if (compareFxAmounts(principal, '0') !== 1 || !/^\d+(?:\.\d{0,6})?$/.test(multiplier)) return null
  return multiplyFxAmount(principal!, multiplier)
}

/** 首页多空合计；任一空头缺少回补估值时，保留未知状态。 */
export function fxHomeHoldings(
  summary: Pick<
    UserSummary,
    'fx_wallets' | 'short_positions' | 'fx_unrealized_pnl' | 'fx_cost_basis'
  > | null,
) {
  const longHoldings = (summary?.fx_wallets ?? []).filter((wallet) => wallet.foreign_amount > 0)
  const shortPositions = summary?.short_positions ?? []
  const longPnl = subtractFxAmounts(summary?.fx_unrealized_pnl ?? 0, '0')
  let shortPnl: string | null = '0.000000'
  let basis = subtractFxAmounts(summary?.fx_cost_basis ?? 0, '0')
  for (const position of shortPositions) {
    const pnl =
      position.reference_cover_cost == null
        ? null
        : subtractFxAmounts(position.proceeds_basis_gold, position.reference_cover_cost)
    shortPnl = shortPnl == null || pnl == null ? null : addFxAmounts(shortPnl, pnl)
    basis = basis == null ? null : addFxAmounts(basis, position.proceeds_basis_gold)
  }
  return {
    longHoldings,
    shortPositions,
    longPnl,
    shortPnl,
    basis,
    pnl: longPnl == null || shortPnl == null ? null : addFxAmounts(longPnl, shortPnl),
  }
}

export function fxHoldingValue(
  wallet: Pick<FxWalletPublic, 'foreign_amount' | 'cost_basis'> | null,
  price: string | null | undefined,
) {
  if (!wallet || price == null) return null
  const marketValue = multiplyFxAmount(wallet.foreign_amount, price)
  if (marketValue === null) return null
  return { marketValue, pnl: subtractFxAmounts(marketValue, wallet.cost_basis) }
}

/** 当前持仓成本参考；空头分母使用含息欠币，不是原始借币数量。 */
export function fxHoldingAveragePrice(
  basis: string | null | undefined,
  quantity: string | null | undefined,
) {
  if (compareFxAmounts(basis, '0') !== 1 || compareFxAmounts(quantity, '0') !== 1) return null
  return divideFxAmount(basis, quantity, 12)
}

/** 报价净所得减对应持仓成本；还债不改变本笔交易盈亏。 */
export function fxSpotClosePreview(
  quote: Pick<FxQuote, 'pair_id' | 'side' | 'input_amount' | 'output_amount'> | null,
  wallet: Pick<FxWalletPublic, 'pair_id' | 'foreign_amount' | 'cost_basis'> | null,
) {
  if (
    !quote ||
    !wallet ||
    quote.side !== 'sell' ||
    quote.pair_id !== wallet.pair_id ||
    compareFxAmounts(quote.output_amount, '0') == null ||
    compareFxAmounts(quote.output_amount, '0') === -1
  )
    return null
  const basis = allocateFxAmount(wallet.cost_basis, quote.input_amount, wallet.foreign_amount)
  if (basis == null) return null
  return {
    basis,
    pnl: subtractFxAmounts(quote.output_amount, basis)!,
    full: compareFxAmounts(quote.input_amount, wallet.foreign_amount) === 0,
  }
}

/** 部分回补按最近持仓快照分摊所得；全回补不受新增欠币利息影响分摊比例。 */
export function fxShortClosePreview(
  quote: Pick<
    FxShortQuote,
    'pair_id' | 'action' | 'cover_all' | 'actual_foreign_amount' | 'input_amount'
  > | null,
  position: Pick<FxShortPosition, 'pair_id' | 'pending_short_debt' | 'proceeds_basis_gold'> | null,
) {
  if (
    !quote ||
    !position ||
    quote.action !== 'cover' ||
    quote.pair_id !== position.pair_id ||
    compareFxAmounts(quote.actual_foreign_amount, '0') !== 1 ||
    compareFxAmounts(quote.input_amount, '0') == null ||
    compareFxAmounts(quote.input_amount, '0') === -1 ||
    compareFxAmounts(position.proceeds_basis_gold, '0') == null ||
    compareFxAmounts(position.proceeds_basis_gold, '0') === -1
  )
    return null
  const basis = quote.cover_all
    ? subtractFxAmounts(position.proceeds_basis_gold, '0')
    : allocateFxAmount(
        position.proceeds_basis_gold,
        quote.actual_foreign_amount,
        position.pending_short_debt,
      )
  if (basis == null) return null
  return { basis, pnl: subtractFxAmounts(basis, quote.input_amount!)!, full: !!quote.cover_all }
}

export function fxGoldPerForeign(trade: {
  side: FxSide
  input_amount: string | null
  output_amount: string | null
}) {
  return trade.side === 'buy'
    ? divideFxAmount(trade.input_amount, trade.output_amount)
    : divideFxAmount(trade.output_amount, trade.input_amount)
}

export function fxShortGoldPerForeign(
  quote: Pick<FxShortQuote, 'action' | 'input_amount' | 'output_amount'>,
) {
  return fxGoldPerForeign({ ...quote, side: quote.action === 'open' ? 'sell' : 'buy' })
}

/** 使用报价的全账户估值，不用本笔倍数推算保证金率。 */
export function fxMarginPercent(
  equity: string | null | undefined,
  basis: string | null | undefined,
) {
  if (compareFxAmounts(equity, '0') == null || compareFxAmounts(basis, '0') == null) return '—'
  if (compareFxAmounts(basis, '0') === 0) return '无风险占用'
  if (compareFxAmounts(basis, '0') !== 1) return '—'
  const ratio = divideFxAmount(equity, basis, 12)
  return ratio == null ? '—' : `${formatFxAmount(multiplyFxAmount(ratio, '100'), 2)}%`
}

/** 按钮金额保留有效小数与千位分隔，不把小额交易四舍五入为零。 */
export function fxOrderAmountLabel(amount: string | null | undefined) {
  const formatted = formatFxAmount(amount)
  const [integer, fraction] = formatted.split('.')
  const significant = fraction?.replace(/0+$/, '')
  return integer!.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (significant ? `.${significant}` : '')
}

export function fxPairAllowsSide(
  pair: Pick<FxPairPublic, 'status' | 'reduce_only'> | null,
  side: FxSide,
): boolean {
  if (!pair) return false
  if (pair.status === 'trading') return side === 'sell' || !pair.reduce_only
  return pair.status === 'paused' && !!pair.reduce_only && side === 'sell'
}

/** Missing unified spendable cash must never expose restricted proceeds as spendable. */
export function fxAvailableCash(summary: Pick<UserSummary, 'cash' | 'available_cash'> | null) {
  if (!summary) return null
  return summary.available_cash ?? null
}

/** 现金差额不等于可借额度；买入投入已包含手续费。 */
export function fxFundingPreview(
  amount: string,
  summary: Pick<UserSummary, 'cash' | 'available_cash'> | null,
) {
  const cash = fxAvailableCash(summary)
  if (
    cash == null ||
    compareFxAmounts(amount, '0') !== 1 ||
    compareFxAmounts(cash, '0') == null ||
    compareFxAmounts(cash, '0') === -1
  )
    return null
  const cashInput = subtractFxAmounts(compareFxAmounts(amount, cash) === 1 ? cash : amount, '0')
  const shortfall = cashInput == null ? null : subtractFxAmounts(amount, cashInput)
  return cashInput == null || shortfall == null ? null : { cashInput, shortfall }
}

/** 仅拦截已知不可执行条件，权威的逐笔风控检查仍由服务端完成。 */
export function fxBuyBlockReason(summary: BuySummary | null, amount: string): string {
  if (!summary) return '账户信息暂不可用，请刷新后买入'
  if (summary.credit_frozen) return '账户已冻结新增信用，请先还款或安全减仓'
  const cash = fxAvailableCash(summary)
  if (compareFxAmounts(cash, '0') === null) return '可用余额暂不可用，请刷新账户'
  if ((compareFxAmounts(cash, '0') ?? 0) <= 0) return '金圆券余额不足，请先补充余额'
  if ((compareFxAmounts(amount, cash) ?? 0) > 0) return '金圆券余额不足，请减少投入金额'
  return ''
}

/** 基于最近账户快照的预览，不可当作实际还债回执。 */
export function fxSellAllocation(output: string, summary: CreditSummary | null) {
  if (!summary) return null
  const debt = summary.debt_with_interest ?? summary.debt
  if (compareFxAmounts(debt, '0') === null || compareFxAmounts(output, '0') === null) return null
  const repayment =
    (compareFxAmounts(debt, '0') ?? 0) <= 0
      ? '0.000000'
      : formatFxAmount((compareFxAmounts(debt, output) ?? 0) > 0 ? output : debt)
  return { repayment, cashIncrease: subtractFxAmounts(output, repayment) }
}

/** Explain actual net legs without guessing from the requested direction. */
export function fxNetActionLabel(legs: ReadonlyArray<{ purpose: string; side: string }>): string {
  const cover = legs.some((l) => l.purpose === 'short_cover')
  const short = legs.some((l) => l.purpose === 'short_open')
  const spot = legs.some((l) => l.purpose === 'spot' || l.purpose === 'borrow_buy')
  if (cover && spot) return '回补后做多'
  if (short && spot) return '平多后开空'
  if (cover) return '回补空头'
  if (short) return '新增空头'
  return legs[0]?.side === 'sell' ? '卖出现货' : '买入外币'
}

export function fxGroupPersonalTrades(rows: readonly FxPersonalTrade[]) {
  const groups: Array<FxPersonalTrade & { legs: FxPersonalTrade[]; actionLabel: string | null }> =
    []
  const byExecution = new Map<number, (typeof groups)[number]>()
  for (const row of rows) {
    const existing = row.execution_id ? byExecution.get(row.execution_id) : undefined
    if (existing) {
      existing.legs.push(row)
      existing.input_amount =
        addFxAmounts(existing.input_amount, row.input_amount) ?? existing.input_amount
      existing.output_amount =
        addFxAmounts(existing.output_amount, row.output_amount) ?? existing.output_amount
      existing.fee_amount = addFxAmounts(existing.fee_amount, row.fee_amount) ?? existing.fee_amount
    } else {
      const group = { ...row, legs: [row], actionLabel: null as string | null }
      groups.push(group)
      if (row.execution_id) byExecution.set(row.execution_id, group)
    }
  }
  for (const group of groups) {
    group.legs.sort((a, b) => (a.execution_leg ?? 0) - (b.execution_leg ?? 0))
    if (group.execution_id) group.actionLabel = fxNetActionLabel(group.legs)
  }
  return groups
}

/** Allocate net buys from account cash; financing can only fund the new long leg. */
export function fxNetFundingPreview(
  amount: string,
  summary: Pick<UserSummary, 'cash' | 'available_cash'> | null,
  quote: {
    legs: readonly Pick<import('@/types/fx').FxNetLeg, 'purpose' | 'side' | 'input_amount'>[]
  } | null,
) {
  const base = fxFundingPreview(amount, summary)
  if (!base || !summary || summary.available_cash == null) return null
  const cover = quote?.legs.find((leg) => leg.purpose === 'short_cover')
  const long = quote?.legs.find(
    (leg) => leg.side === 'buy' && ['spot', 'borrow_buy'].includes(leg.purpose),
  )
  const coverAffordable = !cover || compareFxAmounts(cover.input_amount, summary.available_cash) !== 1
  return { ...base, loanApplicable: !!long && coverAffordable && compareFxAmounts(base.shortfall, '0') === 1 }
}

/** Quick close intent cannot grant permission to open, even after a requote. */
export function fxNetAllowsGrowth(
  quote: Pick<import('@/types/fx').FxNetQuote, 'growth_amount'>,
  closeOnly: boolean,
) {
  return !closeOnly && compareFxAmounts(quote.growth_amount, '0') === 1
}

export function fxNetNewExposure(
  legs: readonly Pick<
    import('@/types/fx').FxNetLeg,
    'purpose' | 'side' | 'input_amount' | 'output_amount'
  >[],
) {
  const opening = legs.find(
    (leg) =>
      leg.purpose === 'short_open' ||
      (leg.side === 'buy' && (leg.purpose === 'spot' || leg.purpose === 'borrow_buy')),
  )
  if (!opening) return null
  return opening.purpose === 'short_open'
    ? { direction: 'short' as const, quantity: opening.input_amount }
    : { direction: 'long' as const, quantity: opening.output_amount }
}

/** Identify newly borrowed assets across net-order legs, including a sell crossing into short. */
export function fxNetBorrowAction(quote: {
  borrow_amount: string
  legs: readonly Pick<import('@/types/fx').FxNetLeg, 'purpose' | 'side' | 'input_amount' | 'output_amount'>[]
}) {
  const shorts = quote.legs.filter(leg => leg.purpose === 'short_open')
  const total = (legs: typeof shorts, field: 'input_amount' | 'output_amount') =>
    legs.reduce((sum, leg) => addFxAmounts(sum, leg[field]) ?? sum, '0')
  if (shorts.length) {
    const quantity = total(shorts, 'input_amount')
    return { kind: 'short' as const, borrowAmount: quantity, quantity, saleProceeds: total(shorts, 'output_amount') }
  }
  if (compareFxAmounts(quote.borrow_amount, '0') === 1) {
    return { kind: 'long' as const, borrowAmount: quote.borrow_amount,
      quantity: total(quote.legs.filter(leg => leg.side === 'buy' && ['spot', 'borrow_buy'].includes(leg.purpose)), 'output_amount'), saleProceeds: null }
  }
  return null
}
