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
