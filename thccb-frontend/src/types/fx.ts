// FX 子游戏前端类型（严格区分玩家公开 schema 与管理员 schema）。
//
// 关键契约：后端 `app/schemas/fx.py` 的 FX schema 直接使用 `Decimal`（未使用
// `Money`/`Price` 的 float PlainSerializer），Pydantic v2 会把 Decimal 序列化成
// JSON 字符串。因此公开/管理响应中的金额一律以 `string` 保存，绝不经过 `Number()`
// 造成精度丢失；格式化与 min-out 计算由 `@/api/fx` 的纯函数完成。
//
// 隐藏字段（target_price / shock_ratio / future_orders / random_state /
// parameter_snapshot 等）只出现在 `*Admin` 类型；玩家公开类型与 SSE 帧类型
// `FxPublicFrame` 只声明白名单字段。

export type FxSide = 'buy' | 'sell'
export type FxPairStatus = 'draft' | 'trading' | 'paused' | 'closed'
export type FxEventStatus =
  | 'draft'
  | 'scheduled'
  | 'published'
  | 'cancelled'
  | 'completed'

// ── 玩家公开 schema（与 FxPairPublic / FxQuote / FxSnapshot / FxTradePublic 对齐） ──

export interface FxPairPublic {
  id: number
  currency_code: string
  currency_name: string
  status: FxPairStatus
  pool_version: number
  created_at: string
  updated_at: string
}

export interface FxSnapshot {
  pair: FxPairPublic
  /** 边际汇率，金圆券 / 1 外币（字符串十进制） */
  price: string
  /** 买入外币有效 ask，金圆券 / 1 外币 = 金入 / 外币出 */
  buy_price: string
  /** 卖出外币有效 bid，金圆券 / 1 外币 = 金出 / 外币入 */
  sell_price: string
  /** buy_price − sell_price，含手续费时为正 */
  spread: string
  volume_24h: string
}

export interface FxQuote {
  pair_id: number
  side: FxSide
  input_amount: string
  output_amount: string
  fee_amount: string
  /** output / input：buy 为「外币/金」，sell 为「金/外币」 */
  effective_price: string
  post_price: string
  expires_at?: string | null
}

export interface FxTradePublic {
  id: number
  pair_id: number
  side: FxSide
  input_amount: string
  output_amount: string
  fee_amount: string
  post_price: string
  created_at: string
}

export interface FxWalletPublic {
  pair_id: number
  /** 当前用户在该 pair 的外币持仓数量 */
  foreign_amount: string
  /** 金圆券成本基础 */
  cost_basis: string
  updated_at?: string | null
}

export interface FxQuoteRequest {
  side: FxSide
  amount: string
}

export interface FxTradeRequest {
  side: FxSide
  amount: string
  /** 客户端可接受的最低产出；服务端在事务内重新报价后低于该值即 409 */
  min_out: string
  idempotency_key: string
}

// ── 公开 SSE 帧（`app/services/fx/market_data.py` 的 _FRAME_KEYS / _NEWS_KEYS） ──

export interface FxPublicNews {
  title?: string
  body?: string
  kind?: string
  published_at?: string
}

/** SSE `event: fx` 的 data；只允许白名单行情与公开新闻字段。 */
export interface FxPublicFrame {
  price?: string
  buy_price?: string
  sell_price?: string
  spread?: string
  volume?: string
  news?: FxPublicNews
}

/** `/chart` 返回的 candle 经归一化后的前端点（数值已转 number）。 */
export interface FxChartPoint {
  t: string
  o: number
  h: number
  l: number
  c: number
  v: number
}

// ── 管理员 schema（仅在 /admin/fx 使用；含 reserves / 隐藏事件参数） ──

export interface FxPairAdmin extends FxPairPublic {
  gold_reserve: string
  foreign_reserve: string
  target_price: string
  initial_price: string
  target_min: string
  target_max: string
  buy_fee_rate: string
  sell_fee_rate: string
}

/** GET /api/v1/admin/fx/pairs 的只读管理视图：含草稿与系统 treasury。 */
export interface FxPairAdminDetail extends FxPairAdmin {
  gold_balance: string
  foreign_balance: string
  daily_spend: string
  spend_date?: string | null
}

export interface FxEventAdmin {
  id: number
  pair_id: number
  status: FxEventStatus
  title: string
  body: string
  kind: string
  published_at?: string | null
  completed_at?: string | null
  // 内部数值参数——只在管理页面渲染，绝不进入玩家 UI 或公开帧。
  shock_ratio?: string | null
  first_reaction_ratio?: string | null
  window_sec?: number | null
  budget?: string | null
  scheduled_at?: string | null
  parameter_snapshot?: Record<string, unknown> | null
  error_message?: string | null
  operator_user_id?: number | null
}

export interface FxIntervention {
  id: number
  pair_id: number
  side: FxSide
  input_amount: string
  output_amount: string
  post_price: string
  source: string
  created_at: string
}

export interface FxPairCreate {
  currency_code: string
  currency_name: string
  status?: FxPairStatus
  gold_reserve?: string
  foreign_reserve?: string
  target_price?: string
  initial_price?: string
  target_min?: string
  target_max?: string
  buy_fee_rate?: string
  sell_fee_rate?: string
}

export interface FxPairPatch {
  currency_code?: string
  currency_name?: string
  status?: FxPairStatus
  target_price?: string
  target_min?: string
  target_max?: string
  buy_fee_rate?: string
  sell_fee_rate?: string
}

export interface FxFundRequest {
  gold_amount?: string
  foreign_amount?: string
}

export interface FxEventCreate {
  pair_id: number
  title: string
  body?: string
  kind?: string
  shock_ratio?: string
  first_reaction_ratio?: string
  window_sec?: number
  budget: string
  scheduled_at?: string | null
}
