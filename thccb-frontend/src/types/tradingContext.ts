/** Mirrors the authenticated backend trading_context whitelist; decimals and versions stay strings. */
export interface GroupKey { product: 'fx' | 'lmsr'; id: number }
export interface GroupStamp { epoch: string; revision: string; input_hash: string }
export interface GroupCoverage extends GroupKey { stamp: GroupStamp }
export interface FxPricingGroup extends GroupCoverage {
  product: 'fx'; as_of: string; gold_reserve: string; foreign_reserve: string
  buy_fee_rate: string; sell_fee_rate: string; pool_version: string
  status: string; archived: boolean; reduce_only: boolean
}
export interface LmsrPricingGroup extends GroupCoverage {
  product: 'lmsr'; as_of: string; outcome_ids: number[]; q_dec: string[]; b: string
  status: string; closes_at: string | null
}
export type PricingGroup = FxPricingGroup | LmsrPricingGroup
export interface LmsrPosition { market_id: number | null; outcome_id: number; amount: string; cost_basis: string }
export interface FxWalletBasis { pair_id: number; foreign_amount: string; cost_basis: string }
export interface FxShortBasis {
  pair_id: number; principal_foreign: string; interest_foreign: string
  interest_last_accrued_at: string | null; restricted_gold: string; proceeds_basis_gold: string
}
export interface AccountBasis {
  user_id: number; economic_version: string; cash: string; debt: string; debt_last_accrued_at: string | null
  credit_frozen: boolean; is_active: boolean; is_bot: boolean; tos_accepted_at: string | null
  last_liquidated_at: string | null; lmsr_positions: LmsrPosition[]; fx_wallets: FxWalletBasis[]; fx_shorts: FxShortBasis[]
}
export interface TradingRules {
  rule_version: string; loan_daily_rate: string; lmsr_sell_fee_rate: string; fx_enabled: boolean
  fx_short_enabled: boolean; loan_enabled: boolean; unified_credit_enabled: boolean
  credit_new_risk_frozen: boolean; risk_engine_ready: boolean
  leverage: string | null; initial_ratio: string | null; maintenance_ratio: string | null
}
export interface TradingContext {
  schema_version: 1; account: AccountBasis; rules: TradingRules; groups: PricingGroup[]
  minimum_group_stamps: GroupCoverage[]; missing_groups: GroupKey[]; as_of: string; server_time: string
}
export interface TradingVersionCheck { economic_version: string; rule_version: string; group_stamps: GroupCoverage[]; missing_groups: GroupKey[]; server_time: string }
export interface TradingPrices { groups: PricingGroup[]; missing_groups: GroupKey[]; server_time: string }
export interface AccountChanged { event_id: string; economic_version: string; reason: string; occurred_at: string; operation_ref?: string | null }
export type PreviewCondition = 'satisfied' | 'rejected' | 'unknown'
export type TradingSessionStatus = 'syncing' | 'ready' | 'stale' | 'error'
