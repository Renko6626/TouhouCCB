// 用户相关类型定义

import type { TitleChip } from '@/api/title'

export interface User {
  id: number
  email: string
  username: string
  cash: number
  debt: number
  is_active: boolean
  is_superuser: boolean
  tos_accepted_at: string | null
}

export interface SummaryPosition {
  outcome_id: number
  market_id: number
  amount: number
  cost_basis: number
}

export interface RankThreshold {
  /** null = 兜底档；判定：命中第一个 null 或 netWorth > min_net_worth 的条目 */
  min_net_worth: number | null
  title: string
}

/** 阶段 3 新契约（spec §6.4）：只有客户端算不出来的东西。
 *  估值/净值/浮盈/rank 由 stores/user.ts 的派生 getters 本地算。 */
export interface UserSummary {
  /** 6dp 全精度——成交后本地 apply 的 cash 基线 */
  cash: number
  debt: number
  positions: SummaryPosition[]
  margin_hard_threshold: number
  margin_soft_threshold: number
  sell_fee_rate: number
  rank_thresholds: RankThreshold[]
  /** 服务端权威（LCV 口径）；本地 marginRatioEstimate 只是显示估算 */
  margin_status: 'healthy' | 'warning' | 'danger'
  liquidation_protected: boolean
  last_liquidated_at: string | null
  equipped_title?: TitleChip | null
}

/** /user/holdings 瘦身后的原始行 */
export interface HoldingSlim {
  market_id: number
  market_title: string
  outcome_id: number
  outcome_label: string
  amount: number
  cost_basis: number
}

/** 客户端派生的持仓视图——字段名与旧 API Holding 完全一致，
 *  Portfolio 表格 / TradePanel 持仓盒零模板改动。估值来自 utils/valuation.ts。 */
export interface Holding extends HoldingSlim {
  avg_price: number
  current_price: number
  /** LCV：含滑点 + 扣 sell_fee；非 TRADING 市场 = 0（"现在卖不出去"） */
  market_value: number
  /** MTM 口径浮盈 */
  unrealized_pnl: number
  /** LCV 口径浮盈；非 TRADING 市场 = -cost_basis */
  unrealized_pnl_liquidation: number
}

/** 市场定价上下文：客户端本地估值/预览的价格来源。
 *  fetchSummary 时全量重建；当前市场由 tick 帧经 patchMarketPrices 续写；
 *  非当前市场允许轻微陈旧——显示口径，权威判定在服务端（spec §6.3）。 */
export interface MarketPriceCtx {
  b: number
  status: string
  title: string
  /** 升序，与 prices 同序（与 tick 帧价格向量的索引契约一致） */
  outcomeIds: number[]
  prices: number[]
  /** 与 outcomeIds 同序 */
  outcomeLabels: string[]
  /** 24h 前价格（列表拉取时 current − price_change_24h 反推；无基准为 null）。
   *  tick 续写 prices 后仍可实时算 24h 涨跌。与 outcomeIds 同序。 */
  prices24hAgo: (number | null)[]
}

export interface Transaction {
  id: number
  outcome_id: number
  market_id?: number | null
  market_title?: string | null
  outcome_label?: string | null
  type: 'buy' | 'sell' | 'settle' | 'settle_lose' | 'liquidate'
  shares: number
  price: number
  gross: number
  fee: number
  cost: number
  timestamp: string
}

// 排行榜相关类型
export type LeaderboardMode = 'net_worth' | 'spending'

export interface LeaderboardItem {
  user_id: number
  username: string
  // 排序分值：net_worth 模式 = cash - debt；spending 模式 = 兑换消费总额 - 当前债务
  net_worth: number
  rank: string
  // spending 模式额外字段
  spent_total?: number | null
  debt?: number | null
  // Task 14：当前佩戴的称号 chip（用于排行榜行内展示）
  equipped_title?: TitleChip | null
}
