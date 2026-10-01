import api from './index'
import type { AccountShortPosition } from '@/types/user'
import type { TitleChip } from './title'

// ===== Types =====

export interface LoanQuota {
  enabled: boolean
  cash: string
  available_cash?: string | null
  restricted_cash?: string
  short_positions?: AccountShortPosition[]
  short_cover_cost?: string | null
  risk_basis?: string | null
  equity_to_risk_basis?: number | null
  risk_status?: 'healthy' | 'warning' | 'danger' | 'blocked' | null
  blocked_reason?: string | null
  debt: string
  net_worth: string | null
  leverage_k: string
  daily_rate: string
  max_borrow: string
  last_accrued_at: string | null
  display_equity?: string | null
  liquidation_equity?: string | null
  r_initial?: string | null
  r_maintenance?: string | null
}

export interface LoanActionResult {
  cash: string
  debt: string
  max_borrow: string
  /** 实际生效金额（repay / repay-all；按提交时的负债与现金封顶） */
  effective?: string | null
}

export interface SiteConfigItem {
  key: string
  value: string
  value_type: string
  updated_at: string
  updated_by: number | null
}

// ===== Player =====

export const loanApi = {
  async quota(): Promise<LoanQuota> {
    return api.get<LoanQuota>('/api/v1/loan/quota')
  },
  async borrow(amount: string): Promise<LoanActionResult> {
    return api.post<LoanActionResult>('/api/v1/loan/borrow', { amount })
  },
  async repay(amount: string): Promise<LoanActionResult> {
    return api.post<LoanActionResult>('/api/v1/loan/repay', { amount })
  },
  async repayAll(): Promise<LoanActionResult> {
    return api.post<LoanActionResult>('/api/v1/loan/repay-all')
  },
}

export interface LiquidationEvent {
  id: number
  user_id: number
  username: string
  triggered_at: string
  pre_cash: number
  pre_debt: number
  pre_holdings_value: number | null
  pre_net_worth: number | null
  /** null when debt was zero at snapshot time */
  pre_margin_ratio: number | null
  sold_positions_count: number
  total_proceeds: number
  repaid_amount: number
  remaining_debt: number
  post_cash: number
  product?: 'lmsr' | 'fx' | null
  trigger_source: string
  fully_liquidated: boolean
  /** 'emergency' = margin 跌破紧急线全平; 'partial' = 渐进按比例平仓 */
  mode: 'emergency' | 'partial'
  /** Task 14：当前佩戴的称号 chip（用于翻车墙行内展示） */
  equipped_title?: TitleChip | null
}

export async function fetchRecentLiquidations(limit = 10): Promise<LiquidationEvent[]> {
  return api.get<LiquidationEvent[]>('/api/v1/loan/recent-liquidations', { params: { limit } })
}

export interface LiquidationPolicy {
  unified_credit_enabled?: boolean
  credit_leverage?: number | null
  r_initial?: number | null
  r_maintenance?: number | null
  sell_fee_rate?: number
  fx_sell_fee_rates?: { pair_id: number; currency_code: string; sell_fee_rate: number }[]
  legacy?: { legacy: true }
  enabled: boolean
  hard_threshold: number       // margin < 这个值触发强平
  soft_threshold: number       // 软警告线
  partial_pct: number          // partial 模式每次卖出仓位比例 (0.10 = 10%)
  target_margin: number        // partial 多 tick 收敛目标
  emergency_threshold: number  // margin < 这个值升级紧急全平
  sweep_interval_sec: number   // sweep 扫描频率（秒）
}

export async function fetchLiquidationPolicy(): Promise<LiquidationPolicy> {
  return api.get<LiquidationPolicy>('/api/v1/loan/liquidation-policy')
}

// ===== Admin =====

export const adminSiteConfigApi = {
  async list(): Promise<SiteConfigItem[]> {
    return api.get<SiteConfigItem[]>('/api/v1/admin/site-config')
  },
  async update(key: string, value: string): Promise<SiteConfigItem> {
    return api.put<SiteConfigItem>(`/api/v1/admin/site-config/${key}`, { value })
  },
}

export default loanApi
