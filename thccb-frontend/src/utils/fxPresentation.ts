import { compareFxAmounts, divideFxAmount, formatFxAmount, multiplyFxAmount, subtractFxAmounts } from '@/api/fx'
import type { FxPairPublic, FxSide, FxTradePublic, FxWalletPublic } from '@/types/fx'
import type { UserSummary } from '@/types/user'

type CreditSummary = Pick<UserSummary, 'debt' | 'debt_with_interest' | 'unified_credit_enabled'>
type BuySummary = CreditSummary & Pick<UserSummary, 'cash' | 'credit_frozen' | 'available_cash'>

export function fxHoldingValue(wallet: Pick<FxWalletPublic, 'foreign_amount' | 'cost_basis'> | null, price: string | null | undefined) {
  if (!wallet || price == null) return null
  const marketValue = multiplyFxAmount(wallet.foreign_amount, price)
  if (marketValue === null) return null
  return { marketValue, pnl: subtractFxAmounts(marketValue, wallet.cost_basis) }
}

export function fxGoldPerForeign(trade: Pick<FxTradePublic, 'side' | 'input_amount' | 'output_amount'>) {
  return trade.side === 'buy'
    ? divideFxAmount(trade.input_amount, trade.output_amount)
    : divideFxAmount(trade.output_amount, trade.input_amount)
}

export function fxPairAllowsSide(pair: Pick<FxPairPublic, 'status' | 'reduce_only'> | null, side: FxSide): boolean {
  if (!pair) return false
  if (pair.status === 'trading') return side === 'sell' || !pair.reduce_only
  return pair.status === 'paused' && !!pair.reduce_only && side === 'sell'
}

/** Missing unified spendable cash must never expose restricted proceeds as spendable. */
export function fxAvailableCash(summary: Pick<UserSummary, 'cash' | 'available_cash' | 'unified_credit_enabled'> | null) {
  if (!summary) return null
  return summary.available_cash ?? (summary.unified_credit_enabled === false ? summary.cash : null)
}

/** 仅拦截已知不可执行条件，权威的逐笔风控检查仍由服务端完成。 */
export function fxBuyBlockReason(summary: BuySummary | null, amount: string): string {
  if (!summary) return '账户信息暂不可用，请刷新后买入'
  if (summary.unified_credit_enabled && summary.credit_frozen) return '账户已冻结新增信用，请先还款或安全减仓'
  if ((compareFxAmounts(summary.debt, '0') ?? 0) > 0) {
    if (!summary.unified_credit_enabled) return '有未还借款，需先还款才能买入外币；仍可卖出'
    if (summary.credit_frozen) return '账户已冻结新增信用，请先还款或安全减仓'
  }
  const cash = fxAvailableCash(summary)
  if (compareFxAmounts(cash, '0') === null) return '可用余额暂不可用，请刷新账户'
  if ((compareFxAmounts(cash, '0') ?? 0) <= 0) return '金圆券余额不足，请先补充余额'
  if ((compareFxAmounts(amount, cash) ?? 0) > 0) return '金圆券余额不足，请减少投入金额'
  return ''
}

/** 基于最近账户快照的预览，不可当作实际还债回执。 */
export function fxSellAllocation(output: string, summary: CreditSummary | null) {
  if (!summary) return null
  const debt = summary.unified_credit_enabled ? summary.debt_with_interest ?? summary.debt : 0
  if (compareFxAmounts(debt, '0') === null || compareFxAmounts(output, '0') === null) return null
  const repayment = (compareFxAmounts(debt, '0') ?? 0) <= 0 ? '0.000000'
    : formatFxAmount((compareFxAmounts(debt, output) ?? 0) > 0 ? output : debt)
  return { repayment, cashIncrease: subtractFxAmounts(output, repayment) }
}
