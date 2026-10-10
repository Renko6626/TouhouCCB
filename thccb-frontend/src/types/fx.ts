// FX 子游戏前端类型（严格区分玩家公开 schema 与管理员 schema）。
//
// 关键契约：后端 `app/schemas/fx.py` 的 FX schema 直接使用 `Decimal`（未使用
// `Money`/`Price` 的 float PlainSerializer），Pydantic v2 会把 Decimal 序列化成
// JSON 字符串。因此公开/管理响应中的金额一律以 `string` 保存，绝不经过 `Number()`
// 造成精度丢失；格式化与 min-out 计算由 `@/api/fx` 的纯函数完成。
//
// 管理员储备字段只出现在 `*Admin` 类型；玩家公开类型与 SSE 帧类型
// `FxPublicFrame` 只声明白名单字段。

export type FxSide = 'buy' | 'sell'
export type FxPairStatus = 'draft' | 'trading' | 'paused' | 'closed'
/** 图表周期：仅玩家页使用，与后端 `/chart` 的 interval 参数一致。 */
export type FxChartInterval = '1m' | '15m' | '1h'
/** 历史/RING 周期：与后端 `RING_SPEC` 的四个档位一致（含 UI 暂未开放的 10s）。 */
export type FxHistoryInterval = '10s' | '1m' | '15m' | '1h'

// ── 玩家公开 schema（与 FxPairPublic / FxQuote / FxSnapshot / FxTradePublic 对齐） ──

export interface FxPairPublic {
  id: number
  currency_code: string
  currency_name: string
  status: FxPairStatus
  reduce_only?: boolean
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

export interface FxPersonalTrade extends FxTradePublic {
  currency_code: string
  currency_name: string
  purpose: string
  is_liquidation: boolean
  borrow_amount?: string | null
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

// ── 公开 SSE 帧（`app/services/fx/market_data.py` 的 _FRAME_KEYS） ──

/** SSE `event: fx` 的 data；只允许白名单行情字段。 */
export interface FxPublicFrame {
  price?: string
  buy_price?: string
  sell_price?: string
  spread?: string
  volume?: string
}

/**
 * `/history/fx/...` 与 SSE `history_tail` 的列式封存段（对齐后端 `HistoryRing`）。
 *
 * 价格与成交量都是十进制字符串（保留 Decimal），**只有图表边界才转 number**。
 * 绝不能复用 LMSR 的 `price × 1e8` 整数编码：FX 汇率可能超出 JS 安全整数。
 * 稀疏桶用 `t[i]`（相对 t0 的 step 偏移）定位；所有列长度必须一致。
 */
export interface FxHistorySegment {
  /** 段起点 epoch 秒（已对齐段长，封闭段不可变） */
  t0: number
  /** 桶宽（秒） */
  step: number
  /** 本段桶总数（含空桶） */
  n_buckets: number
  /** 有成交桶的偏移（相对 t0，单位 step），0 <= t[i] < n_buckets */
  t: number[]
  o: string[]
  h: string[]
  l: string[]
  c: string[]
  /** 金侧成交量 Decimal 字符串，非负 */
  v: string[]
  /** 每桶成交笔数，非负整数 */
  trades: number[]
}

/** SSE 首包 `history_tail`：interval → 该档封存边界到当前桶的尾巴。 */
export type FxHistoryTail = Partial<Record<FxHistoryInterval, FxHistorySegment>>

/** SSE `data.trades[]` 的公开逐笔成交，用于按真实成交时间增量更新 OHLCV。 */
export interface FxTradeTick {
  /** FxTrade.id（安全整数），用于去重 */
  id: number
  /** 真实成交时间 ISO UTC（不是客户端接收时间） */
  ts: string
  /** 成交后边际汇率 Decimal 字符串 */
  post_price: string
  /** 金侧成交量 Decimal 字符串（buy 金入 / sell 金出） */
  gold_volume: string
}

/**
 * SSE `event: fx` / `snapshot` 的完整公开信封：报价字段 + 历史与逐笔成交扩展。
 * 私有字段（gold_reserve / foreign_reserve /
 * parameter_snapshot 等）不在白名单内，解析时严格丢弃。
 */
export interface FxPublicEnvelope extends FxPublicFrame {
  /** 后端历史是否已就绪；false 时前端只走 `/chart` 回退 */
  history_ready?: boolean
  /** 历史版本 UUID；变化即代表缓存需要整套重读 */
  history_version?: string
  history_tail?: FxHistoryTail
  /** 尾巴生成时刻 ISO UTC，用于 freshness 判定 */
  history_tail_at?: string
  /** 尾巴已覆盖到的最后成交 id；<= 此值的实时成交必须跳过（避免重复累计） */
  history_tail_through_trade_id?: number
  /** 增量帧内的公开成交（带真实时间与金侧量） */
  trades?: FxTradeTick[]
  /** gap/溢出导致尾段失效：只需补尾段，不要重读封存段 */
  history_invalidated?: boolean
}

/**
 * `loadFxHistoryCandles` 需要的快照尾段上下文，由 `FxStream` envelope 组装。
 * 字段允许为 null，表示当前没有可用尾段/版本。
 */
export interface FxHistorySnapshotTail {
  history_version: string | null
  history_tail: FxHistoryTail | null
  history_tail_at: string | null
  history_tail_through_trade_id: number | null
  history_ready?: boolean
  history_invalidated?: boolean
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

/** FX 实时价格帧经页面转成的图表 tick（价格保持字符串语义）。 */
export interface FxPriceTick {
  /** 边际汇率，金圆券 / 1 外币（字符串十进制，避免 Number 破坏精度） */
  price: string
  /** 客户端接收时刻（ms），用于归类到当前 bucket */
  ts: number
}

// ── 管理员 schema（仅在 /admin/fx 使用；含 reserves） ──

export interface FxPairAdmin extends FxPairPublic {
  archived?: boolean
  short_lending_limit_foreign?: string
  gold_reserve: string
  foreign_reserve: string
  initial_price: string
  buy_fee_rate: string
  sell_fee_rate: string
}

/** GET /api/v1/admin/fx/pairs 的只读管理视图：含草稿与系统 treasury。 */
export interface FxPairAdminDetail extends FxPairAdmin {
  gold_balance: string
  foreign_balance: string
}


export interface FxPairCreate {
  short_lending_limit_foreign?: string
  currency_code: string
  currency_name: string
  status?: FxPairStatus
  gold_reserve?: string
  foreign_reserve?: string
  initial_price?: string
  buy_fee_rate?: string
  sell_fee_rate?: string
}

export interface FxPairPatch {
  short_lending_limit_foreign?: string
  currency_code?: string
  currency_name?: string
  status?: FxPairStatus
  buy_fee_rate?: string
  sell_fee_rate?: string
}

export interface FxFundRequest {
  gold_amount?: string
  foreign_amount?: string
}

export interface FxShortPosition {
  pair_id: number
  currency_code: string
  principal_foreign: string
  interest_foreign: string
  pending_short_debt: string | null
  restricted_gold: string
  proceeds_basis_gold: string
  reference_cover_cost: string | null
  reference_cover_fee: string | null
  executable: boolean
  risk_status: string
  blocked_reason: string | null
}
export interface FxShortQuoteRequest {
  action: 'open' | 'cover'
  foreign_amount?: string
  cover_all?: boolean
}
export interface FxShortQuote {
  pair_id: number
  action: 'open' | 'cover'
  purpose: string
  requested_foreign_amount: string | null
  cover_all: boolean | null
  actual_foreign_amount: string | null
  input_amount: string | null
  output_amount: string | null
  fee_amount: string | null
  fee_currency: string
  restricted_gold_delta: string | null
  available_cash: string | null
  affordable: boolean | null
  estimated_equity: string | null
  estimated_risk_basis: string | null
  margin_status: 'healthy' | 'warning' | 'danger' | 'blocked'
  risk_status: string
  risk_blocked_reason: string | null
  executable: boolean
  blocked_reason: string | null
  expires_at: string
}
export interface FxShortTrade extends Omit<FxTradePublic, 'id'> {
  trade_id: number
  purpose: string
  replay: boolean
}

export interface FxBorrowBuyQuoteRequest { amount: string; borrow_amount: string }
export interface FxBorrowBuyRequest extends FxBorrowBuyQuoteRequest {
  min_out: string
  idempotency_key: string
}
export interface FxBorrowBuyQuote {
  borrow_amount: string
  pair_id: number
  input_amount: string
  cash_amount: string
  output_amount: string | null
  fee_amount: string | null
  effective_price: string | null
  post_price: string | null
  available_cash: string | null
  affordable: boolean | null
  estimated_debt: string | null
  estimated_equity: string | null
  estimated_risk_basis: string | null
  equity_to_risk_basis: string | null
  margin_status: 'healthy' | 'warning' | 'danger' | 'blocked'
  executable: boolean
  blocked_reason: string | null
  leverage: string | null
  daily_rate: string | null
  r_initial: string | null
  r_maintenance: string | null
  expires_at: string
}
export interface FxBorrowBuyTrade {
  trade_id: number
  pair_id: number
  input_amount: string
  borrow_amount: string
  cash_amount: string
  output_amount: string
  fee_amount: string
  post_price: string
  replay: boolean
  created_at: string
}
