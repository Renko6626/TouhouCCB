// FX 玩家/管理 API 封装 + 可测试纯函数（金额格式化、min-out/滑点、SSE 白名单、
// 错误映射）。所有金额保持字符串/十进制语义，不用 Number() 破坏 6 位精度。
import api from './index'
import { fetchSiteResource, openSiteEventSource, type SiteEventSource } from './siteTransport'
import { FxPendingOrder, type FxPendingStorage } from './fxPendingOrder'
import type {
  FxTriggerOrderCreate,
  FxTriggerOrder,
  FxTriggerStatus,
  FxBorrowBuyQuote,
  FxBorrowBuyQuoteRequest,
  FxBorrowBuyRequest,
  FxBorrowBuyTrade,
  FxShortPosition,
  FxShortQuote,
  FxShortQuoteRequest,
  FxShortTrade,
  FxFundRequest,
  FxAdminOverview,
  FxLiquidityPreviewRequest,
  FxLiquidityPreview,
  FxLiquidityHistory,
  FxPersonalTrade,
  FxPairAdmin,
  FxPairAdminDetail,
  FxPairCreate,
  FxPairPatch,
  FxPairPublic,
  FxPublicFrame,
  FxPublicEnvelope,
  FxChartPoint,
  FxHistoryInterval,
  FxHistorySegment,
  FxQuote,
  FxQuoteRequest,
  FxSide,
  FxSnapshot,
  FxTradePublic,
  FxTradeRequest,
  FxWalletPublic,
} from '@/types/fx'
import {
  isFxHistoryInterval,
  sanitizeFxHistorySegment,
  sanitizeFxHistoryTail,
  sanitizeFxTradeTicks,
  sanitizeFxEventTicks,
} from '@/utils/fxHistory'

export const FX_EMPTY = '—'

// ── SSE 公开帧白名单（镜像 market_data._FRAME_KEYS） ──
export const FX_PUBLIC_FRAME_KEYS = [
  'price',
  'buy_price',
  'sell_price',
  'spread',
  'volume',
] as const

// 后端 detail → 中文提示。动态文案（如首轮失败原因）走 fallback。
const FX_ERROR_DETAILS: Record<string, string> = {
  '最多只能同时保留 3 张有效挂单': '全账户最多同时保留 3 张等待挂单，请先撤销或等待成交',
  'FX pair is closed': '该币种已关闭，无法创建挂单',
  'FX pair is permanently unavailable': '该币种已关闭或归档，挂单已结束',
  'Order not found': '挂单不存在，请刷新记录',
  'Order is already finished': '挂单已结束，请刷新记录查看结果',
  'no long position to close': '到价时已无多头仓位可平',
  'long close quantity is too small': '要平掉的多头数量太小，请调整比例',
  'no short position to cover': '到价时已无空头仓位可平',
  'no outstanding short to cover': '到价时已无空头仓位可平',
  'auto borrow limit exceeded': '所需自动借款超过设定上限，请调整投入金额或借款上限',
  'short close quantity is too small': '要平掉的空头数量太小，请调整比例',
  'User is inactive': '账户当前不可交易',
  risk_engine_unavailable: '信用风控暂不可用，无法借款买入',
  loan_disabled: '借款功能已暂停，仍可使用现金买入',
  'economic_version_changed; requote': '账户已变化，请重新报价后确认',
  'growth_requires_confirmation; requote': '新的报价包含开仓，请重新确认交易结果',
  interest_exact_cover_uncertainty: '本笔贴近回补或风控边界，请调整金额或先全部回补',
  'insufficient cash to cover': '现金不足以完成回补',
  same_pair_short: '请先回补当前币种的空头，再借款买入',
  incomplete_asset_valuation: '账户持仓暂无法完整估值，请更新账户和报价',
  'FX trading is disabled': 'FX 交易总闸未开启，管理员开市后才能交易',
  'FX pair is not trading': '该货币对当前暂停或未开市，无法交易',
  'FX pair is reduce-only': '当前币种只允许减仓，跨零开仓暂不可用',
  insufficient_initial_margin: '本笔买入后不满足初始保证金要求，请减少投入或先还款',
  credit_frozen: '账户已冻结新增信用，请先还款或安全减仓',
  frozen_by_operator: '当前暂停新增信用风险，暂不能负债买入；仍可还款或安全减仓',
  version_conflict: '账户或行情已变化，请刷新后重新确认报价',
  'version_conflict; retry': '账户或行情已变化，请刷新后重新确认报价',
  'FX pair not found': 'FX 货币对不存在',
  'bot accounts cannot trade FX': '机器人账户不能参与 FX 交易',
  'TOS acceptance required': '请先同意用户协议后再交易',
  'outstanding debt blocks FX purchases': '有未还借款时不能买入外币（仍可卖出）',
  'insufficient cash': '金圆券余额不足',
  'insufficient FX wallet balance': '外币持仓不足',
  'quoted output is below min_out': '实际所得低于最低可接受金额，交易已取消，请查看新报价后重试',
  'idempotency key parameter mismatch': '重复提交的参数与首次不一致，已拒绝',
  'only one trading FX pair is allowed': '同一时间只允许一个处于交易状态的货币对',
  'currency cannot be changed after opening': '开市后不能修改币种代码，名称仍可修改',
  'opened FX pair cannot return to draft': '已开市的市场不能退回草稿，请使用暂停、关闭或归档',
  'currency code already exists': '币种代码已存在，请使用其他代码',
  'pair fields cannot be null': '请填写完整的货币对参数',
  'FX pair is archived': '该市场已归档，无法继续修改或运营',
  'FX pair has outstanding holdings': '仍有玩家持仓或成本余额，请清空后再删除或归档',
  'FX pair has history; archive it instead': '该市场已有成交历史，请使用归档保留账目',
  'fee rate must be between 0 and 1': '费率必须在 0 与 1 之间',
  'withdrawal would exhaust reserves': '撤资会使池子储备低于安全下限',
  'withdrawal exceeds treasury balance': '撤资金额超过系统储备余额',
  'fund amount must be positive': '注资金额必须为正数',
  'unknown FX config key': '未知的 FX 配置项',
}

// ── 金额格式化（十进制字符串，half-up，不丢精度） ──

/**
 * 把 JS `Number` 可能产生的科学计数法字符串（如 `1e-7`、`1.23e+21`）展开为
 * 普通十进制字符串，供字符串版 formatter 使用。非科学计数法原样返回；
 * 无法安全展开时返回 null。
 *
 * 只有「lightweight-charts 必须要 number」的图表适配层才会把价格先变成
 * number，再经这里还原成字符串格式化，避免精度在展示前丢失。
 */
export function expandExponential(raw: string): string | null {
  const trimmed = raw.trim()
  const match = /^([+-]?)(\d+)(?:\.(\d+))?[eE]([+-]?\d+)$/.exec(trimmed)
  if (!match) return trimmed
  const sign = match[1] === '-' ? '-' : ''
  const intDigits = match[2]!
  const fracDigits = match[3] ?? ''
  const exponent = Number(match[4])
  if (!Number.isFinite(exponent) || Math.abs(exponent) > 200) return null
  const digits = intDigits + fracDigits
  const point = intDigits.length + exponent
  let body: string
  if (point <= 0) {
    body = `0.${'0'.repeat(-point)}${digits}`
  } else if (point >= digits.length) {
    body = `${digits}${'0'.repeat(point - digits.length)}`
  } else {
    body = `${digits.slice(0, point)}.${digits.slice(point)}`
  }
  return `${sign}${body}`
}

function incrementScaled(intPart: string, fracPart: string): { int: string; frac: string } {
  const digits = (intPart + fracPart).split('')
  let i = digits.length - 1
  while (i >= 0) {
    const d = digits[i]!
    if (d === '9') {
      digits[i] = '0'
      i -= 1
    } else {
      digits[i] = String(Number(d) + 1)
      break
    }
  }
  if (i < 0) digits.unshift('1')
  const intLen = digits.length - fracPart.length
  return { int: digits.slice(0, intLen).join(''), frac: digits.slice(intLen).join('') }
}

/**
 * 把十进制字符串/数字格式化为固定小数位（half-up），全程字符串运算，
 * 避免 `Number()` 在 1e15 级金额上丢精度。非法输入返回 `—`。
 */
export function formatFxAmount(value: string | number | null | undefined, digits = 6): string {
  if (value === null || value === undefined) return FX_EMPTY
  const expanded = expandExponential(String(value))
  if (expanded === null) return FX_EMPTY
  const raw = expanded.trim()
  if (raw === '' || raw === '-' || raw === '.' || raw === '-.') return FX_EMPTY
  if (!/^-?\d*(?:\.\d*)?$/.test(raw)) return FX_EMPTY

  const d = Math.max(0, Math.min(12, Math.floor(digits)))
  let sign = ''
  let body = raw
  if (body.startsWith('-')) {
    sign = '-'
    body = body.slice(1)
  }
  const dot = body.indexOf('.')
  let intPart = dot >= 0 ? body.slice(0, dot) : body
  const fracPart = dot >= 0 ? body.slice(dot + 1) : ''
  if (intPart === '') intPart = '0'
  intPart = intPart.replace(/^0+(?=\d)/, '')

  const roundFrac = (fracPart + '0'.repeat(d + 1)).slice(0, d + 1)
  const keep = roundFrac.slice(0, d)
  const roundDigit = d < roundFrac.length ? roundFrac.charCodeAt(d) - 48 : 0
  if (roundDigit >= 5) {
    const inc = incrementScaled(intPart, keep)
    return `${sign}${inc.int}${d > 0 ? '.' + inc.frac : ''}`
  }
  return `${sign}${intPart}${d > 0 ? '.' + keep : ''}`
}

// ── FX 价格精度：汇率比普通市场敏感，按数量级动态选择小数位 ──

/** 价格默认小数位（对应 minMove = 1e-6）。 */
export const FX_PRICE_BASE_DECIMALS = 6
/** 价格显示小数位上限（防御性，避免极小价格把格式化字符串拉爆）。 */
export const FX_PRICE_MAX_DECIMALS = 12
/** 目标有效数字：0.2 → 6 位；0.000123 → 9 位；12.345678 → 6 位。 */
export const FX_PRICE_SIGNIFICANT_DIGITS = 6

export interface FxPricePrecision {
  precision: number
  minMove: number
}

export interface FxPriceDecimalOptions {
  base?: number
  max?: number
  significant?: number
}

/** 从已归一化的十进制字符串求 floor(log10(|value|))；零/非法返回 null。 */
function decimalExponent(raw: string): number | null {
  const match = /^-?(\d*)(?:\.(\d*))?$/.exec(raw)
  if (!match) return null
  const intPart = (match[1] ?? '').replace(/^0+/, '')
  if (intPart.length > 0) return intPart.length - 1
  const fracPart = match[2] ?? ''
  const firstSignificant = fracPart.search(/[1-9]/)
  if (firstSignificant < 0) return null
  return -(firstSignificant + 1)
}

/**
 * 动态价格小数位：默认 6 位，按数量级扩展到约 6 位有效数字，最多 12 位。
 * - `0.2` → 6（`0.200000`）
 * - `0.000123` → 9（`0.000123000`）
 * - `12.345678` → 6
 * - `1e-9` → 12（触顶）
 * 非法值 / 零返回默认 6。
 */
export function fxPriceDecimals(
  value: string | number | null | undefined,
  options: FxPriceDecimalOptions = {},
): number {
  const base = Math.max(0, Math.min(12, Math.floor(options.base ?? FX_PRICE_BASE_DECIMALS)))
  const max = Math.max(base, Math.min(12, Math.floor(options.max ?? FX_PRICE_MAX_DECIMALS)))
  const significant = Math.max(1, Math.floor(options.significant ?? FX_PRICE_SIGNIFICANT_DIGITS))
  if (value === null || value === undefined) return base
  const expanded = expandExponential(String(value))
  if (expanded === null) return base
  const exponent = decimalExponent(expanded.trim())
  if (exponent === null) return base
  return Math.max(base, Math.min(max, significant - 1 - exponent))
}

/** 价格专用格式化：与图表右轴 / tooltip 共用同一 formatter。 */
export function formatFxPrice(
  value: string | number | null | undefined,
  options: FxPriceDecimalOptions = {},
): string {
  return formatFxAmount(value, fxPriceDecimals(value, options))
}

/**
 * lightweight-charts 价格轴格式：`precision` 控制标签小数位，`minMove` 是价格步长。
 * 默认 1e-6，极小汇率才细化到 `10^-precision`。
 */
export function fxPricePrecision(
  value: string | number | null | undefined,
  options: FxPriceDecimalOptions = {},
): FxPricePrecision {
  const precision = fxPriceDecimals(value, options)
  return { precision, minMove: 10 ** -precision }
}

function parseScaled(value: string | number | null | undefined, scale: number): bigint | null {
  if (value === null || value === undefined) return null
  const expanded = expandExponential(String(value))
  if (expanded === null) return null
  const raw = expanded.trim()
  if (raw === '' || raw === '-' || raw === '.' || raw === '-.') return null
  if (!/^-?\d*(?:\.\d*)?$/.test(raw)) return null
  let sign = 1n
  let body = raw
  if (body.startsWith('-')) {
    sign = -1n
    body = body.slice(1)
  }
  const dot = body.indexOf('.')
  const intPart = (dot >= 0 ? body.slice(0, dot) : body) || '0'
  const fracPart = dot >= 0 ? body.slice(dot + 1) : ''
  const frac = (fracPart + '0'.repeat(scale)).slice(0, scale)
  const combined = `${intPart}${frac}`.replace(/^0+(?=\d)/, '')
  return sign * BigInt(combined === '' ? '0' : combined)
}

function scaledToString(value: bigint, scale: number): string {
  const neg = value < 0n
  const v = neg ? -value : value
  let s = v.toString()
  if (scale > 0) s = s.padStart(scale + 1, '0')
  const intPart = scale > 0 ? s.slice(0, s.length - scale) : s
  const fracPart = scale > 0 ? s.slice(s.length - scale) : ''
  return `${neg ? '-' : ''}${intPart}${scale > 0 ? '.' + fracPart : ''}`
}

/** 金额比较保持十进制语义；非法输入不参与比较。 */
export function compareFxAmounts(
  a: string | number | null | undefined,
  b: string | number | null | undefined,
): number | null {
  const left = parseScaled(a, 6)
  const right = parseScaled(b, 6)
  if (left === null || right === null) return null
  return left < right ? -1 : left > right ? 1 : 0
}

export function subtractFxAmounts(a: string | number, b: string | number): string | null {
  const left = parseScaled(a, 6)
  const right = parseScaled(b, 6)
  return left === null || right === null ? null : scaledToString(left - right, 6)
}

export function addFxAmounts(a: string | number, b: string | number): string | null {
  const left = parseScaled(a, 6)
  const right = parseScaled(b, 6)
  return left === null || right === null ? null : scaledToString(left + right, 6)
}

/** 仅用于账面估值，数量 6dp × 汇率 12dp，结果截断到资金 6dp。 */
export function multiplyFxAmount(amount: string, price: string): string | null {
  const quantity = parseScaled(amount, 6)
  const rate = parseScaled(price, 12)
  return quantity === null || rate === null
    ? null
    : scaledToString((quantity * rate) / 10n ** 12n, 6)
}

/** 按数量分摊六位金额，一次整数乘除后截断，避免中间比率损失精度。 */
export function allocateFxAmount(
  amount: string | null | undefined,
  quantity: string | null | undefined,
  total: string | null | undefined,
): string | null {
  const basis = parseScaled(amount, 6)
  const part = parseScaled(quantity, 6)
  const whole = parseScaled(total, 6)
  if (
    basis === null ||
    part === null ||
    whole === null ||
    basis < 0n ||
    part <= 0n ||
    whole <= 0n ||
    part > whole
  )
    return null
  return scaledToString((basis * part) / whole, 6)
}

function clampBps(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.max(0, Math.min(10000, Math.trunc(value)))
}

/**
 * 由报价产出与最大滑点（bps）计算服务端 `min_out`：向下取 6 位（保守）。
 * 向下取整保证不会因为客户端四舍五入而放宽服务端滑点保护。
 */
export function computeMinOut(
  output: string | number | null | undefined,
  slippageBps: number,
): string {
  const scaled = parseScaled(output, 6)
  if (scaled === null) return '0.000000'
  const bps = clampBps(slippageBps)
  const result = (scaled * BigInt(10000 - bps)) / 10000n
  return scaledToString(result, 6)
}

/**
 * 十进制字符串除法（截断到 `digits` 位小数），用于「平均成本 = 成本 / 持仓」这类
 * 派生比值，避免为了显示而先把金额 `Number()`。除零 / 非法输入返回 null。
 */
export function divideFxAmount(
  numerator: string | number | null | undefined,
  denominator: string | number | null | undefined,
  digits = 12,
): string | null {
  const scale = Math.max(0, Math.min(12, Math.floor(digits)))
  // FX rates can have twelve decimals even when the resulting quantity has six.
  const guard = 12
  const n = parseScaled(numerator, scale + guard)
  const d = parseScaled(denominator, guard)
  if (n === null || d === null || d === 0n) return null
  return scaledToString(n / d, scale)
}

function toFiniteNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined) return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

/** 有效价相对中间价的滑点（bps，绝对值）。中间价非法时返回 null。 */
export function tradeSlippageBps(
  midPrice: string | number | null | undefined,
  effectivePrice: string | number | null | undefined,
): number | null {
  const mid = toFiniteNumber(midPrice)
  const effective = toFiniteNumber(effectivePrice)
  if (mid === null || effective === null || mid <= 0) return null
  return (Math.abs(effective - mid) / mid) * 10000
}

// ── SSE 帧白名单：只保留公开行情字段 ──

function pickString(source: Record<string, unknown>, key: string): string | undefined {
  const value = source[key]
  if (value === undefined || value === null) return undefined
  if (typeof value === 'string') return value
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return undefined
}

/**
 * 把任意对象收敛到公开白名单。隐藏字段（reserves/parameter_snapshot 等）不在白名单内，天然被丢弃。
 */
export function sanitizeFxFrame(raw: unknown): FxPublicFrame | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null
  const source = raw as Record<string, unknown>
  const frame: FxPublicFrame = {}
  for (const key of FX_PUBLIC_FRAME_KEYS) {
    const value = pickString(source, key)
    if (value !== undefined) frame[key] = value
  }
  return frame
}

/** 解析直接的公开帧 JSON 字符串。 */
export function parseFxFrame(raw: string | null | undefined): FxPublicFrame | null {
  if (!raw) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  return sanitizeFxFrame(parsed)
}

/**
 * 解析 SSE `data:` 行的 JSON：既接受 `{data: frame}` 信封，也接受裸帧。
 * 信封里的 `type/market_id/seq` 等元数据不会进入返回对象。
 */
export function parseFxSsePayload(raw: string | null | undefined): FxPublicFrame | null {
  if (!raw) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  if (typeof parsed === 'object' && parsed !== null && 'data' in (parsed as object)) {
    return sanitizeFxFrame((parsed as { data?: unknown }).data)
  }
  return sanitizeFxFrame(parsed)
}

/**
 * 把任意对象收敛到完整公开信封：旧报价白名单 + 新增历史/逐笔成交字段。
 * 私有字段（gold_reserve/foreign_reserve/parameter_snapshot
 * 等）不在白名单内，解析时严格丢弃。
 */
export function sanitizeFxEnvelope(raw: unknown): FxPublicEnvelope | null {
  const frame = sanitizeFxFrame(raw)
  if (!frame) return null
  const source = raw as Record<string, unknown>
  const envelope: FxPublicEnvelope = { ...frame }

  if (typeof source.history_ready === 'boolean') envelope.history_ready = source.history_ready
  if (typeof source.history_version === 'string' && source.history_version) {
    envelope.history_version = source.history_version
  }
  const tail = sanitizeFxHistoryTail(source.history_tail)
  if (tail) envelope.history_tail = tail
  const tailAt = source.history_tail_at
  if (typeof tailAt === 'string' && !Number.isNaN(new Date(tailAt).getTime())) {
    envelope.history_tail_at = tailAt
  }
  const through = source.history_tail_through_event_id
  if (typeof through === 'number' && Number.isSafeInteger(through) && through >= 0) {
    envelope.history_tail_through_event_id = through
  }
  const events = sanitizeFxEventTicks(source.events)
  if (events.length > 0) envelope.events = events
  const trades = sanitizeFxTradeTicks(source.trades)
  if (trades.length > 0) envelope.trades = trades
  if (typeof source.history_invalidated === 'boolean') {
    envelope.history_invalidated = source.history_invalidated
  }
  return envelope
}

/**
 * 解析 SSE `data:` 行的 JSON 为完整信封：既接受 `{data: frame}` 信封，也接受裸帧。
 * 与 `parseFxSsePayload` 保持同一层解包逻辑，但额外暴露历史版本/尾段/逐笔成交。
 */
export function parseFxSseEnvelope(raw: string | null | undefined): FxPublicEnvelope | null {
  if (!raw) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  if (typeof parsed === 'object' && parsed !== null && 'data' in (parsed as object)) {
    return sanitizeFxEnvelope((parsed as { data?: unknown }).data)
  }
  return sanitizeFxEnvelope(parsed)
}

// ── 图表归一化（/chart 无 response_model，FastAPI 会把 Decimal 转 float） ──

function chartNumber(value: unknown): number {
  if (typeof value !== 'string' && typeof value !== 'number') throw new Error('FX chart contains an unknown value')
  const n = Number(value)
  if (!Number.isFinite(n) || n < 0 || value === '') throw new Error('FX chart contains an invalid value')
  return n
}

export function normalizeFxCandles(raw: unknown): FxChartPoint[] {
  if (!Array.isArray(raw)) return []
  const points: FxChartPoint[] = []
  for (const row of raw) {
    if (typeof row !== 'object' || row === null) continue
    const r = row as Record<string, unknown>
    const t =
      typeof r.bucket_start === 'string' ? r.bucket_start : typeof r.t === 'string' ? r.t : null
    if (!t) continue
    if (!Number.isSafeInteger(r.n_events) || (r.n_events as number) < 0
      || !Number.isSafeInteger(r.n_trades) || (r.n_trades as number) < 0 || (r.n_trades as number) > (r.n_events as number)) {
      throw new Error("FX chart response is missing event counts; refresh to the matching version")
    }
    points.push({
      t,
      o: chartNumber(r.open),
      h: chartNumber(r.high),
      l: chartNumber(r.low),
      c: chartNumber(r.close),
      v: chartNumber(r.volume),
      n_events: r.n_events as number,
      n_trades: r.n_trades as number,
    })
  }
  return points.sort((a, b) => new Date(a.t).getTime() - new Date(b.t).getTime())
}

// ── 公开历史/图表取数（/history/fx/...、/chart + 元数据头） ──
//
// 这两条是**公开缓存**请求，故意用原生 fetch：不挂 axios 拦截器的 auth / JSON
// content-type（保留 `api` 拦截器给其它端点）。历史段 URL 含 history_version，
// 版本变化即整套重读；`/chart` 的响应头给出该响应覆盖到的成交游标与版本。

function fxPublicBaseUrl(): string {
  return (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8004').replace(/\/$/, '')
}

/** `/chart` 公开响应：body 归一化点 + 该响应覆盖到的游标（来自 additive 响应头）。 */
export interface FxChartMeta {
  points: FxChartPoint[]
  /** X-FX-Through-Event-ID：响应已覆盖到的最后价格事件 id */
  throughEventId: number
  /** X-FX-History-Version：响应所属历史版本 */
  historyVersion: string
}

function requiredSafeIntHeader(headers: Headers, name: string): number | null {
  const rawValue = headers.get(name)
  if (rawValue === null || rawValue.trim() === '') return null
  const value = Number(rawValue)
  return Number.isSafeInteger(value) && value >= 0 ? value : null
}

/**
 * 旧 `/chart` 公开端点 + 历史元数据头（body 结构不变）。
 * 缺任一响应头（后端尚未上线 additive 头 / 未就绪）→ 显式抛错，绝不把旧
 * SSE 游标当作“保护了更新的数据”。
 */
export async function getChartWithMeta(
  pairId: number,
  interval: string,
  fromIso: string,
  toIso: string,
  signal?: AbortSignal,
): Promise<FxChartMeta> {
  const params = new URLSearchParams({ interval, from: fromIso, to: toIso })
  const resp = await fetchSiteResource(
    `${fxPublicBaseUrl()}/api/v1/fx/pairs/${pairId}/chart?${params.toString()}`,
    { signal },
  )
  if (!resp.ok) throw new Error(`FX chart failed: ${resp.status}`)
  const throughEventId = requiredSafeIntHeader(resp.headers, 'X-FX-Through-Event-ID')
  const historyVersion = resp.headers.get('X-FX-History-Version')
  if (throughEventId === null || !historyVersion) {
    throw new Error('FX chart response is missing history metadata')
  }
  return { points: normalizeFxCandles(await resp.json()), throughEventId, historyVersion }
}

/**
 * 取一段不可变封存历史（登录时携带现有凭证，保留管理员访问）。
 * - 404（尚未封存/竞态窗口）→ null，调用方按不完整处理；
 * - 503（后端历史未就绪）或结构非法/与请求 interval·segment 错位 → 抛出，让调用方
 *   显式回退 `/chart`，不静默吞错。
 */
export async function fetchFxHistorySegment(
  pairId: number,
  historyVersion: string,
  interval: FxHistoryInterval,
  segmentEpoch: number,
  signal?: AbortSignal,
): Promise<FxHistorySegment | null> {
  if (!Number.isSafeInteger(pairId) || pairId <= 0) return null
  if (!historyVersion || !isFxHistoryInterval(interval)) return null
  if (!Number.isSafeInteger(segmentEpoch) || segmentEpoch < 0) return null
  const path = `${fxPublicBaseUrl()}/history/fx/${pairId}/${encodeURIComponent(historyVersion)}/${interval}/${segmentEpoch}.json`
  const resp = await fetchSiteResource(path, { signal })
  if (resp.status === 404) return null
  if (!resp.ok) throw new Error(`FX history segment ${segmentEpoch} failed: ${resp.status}`)
  const segment = sanitizeFxHistorySegment(await resp.json(), { interval, t0: segmentEpoch })
  if (!segment) throw new Error(`invalid FX history segment ${segmentEpoch}`)
  return segment
}

// ── 错误映射 ──

interface FxErrorLike {
  status?: number
  message?: string
  data?: { detail?: unknown }
}

/** 成交/操作是否因行情或幂等冲突返回 409（页面需据此刷新 snapshot）。 */
export function isConflictError(err: unknown): boolean {
  return (err as FxErrorLike | null)?.status === 409
}

export function mapFxError(err: unknown, fallback = '请求失败'): string {
  const e = (err ?? {}) as FxErrorLike
  const detail = typeof e.data?.detail === 'string' ? e.data.detail : ''
  if (detail && FX_ERROR_DETAILS[detail]) return FX_ERROR_DETAILS[detail]
  if (detail && e.status === 422) return detail

  switch (e.status) {
    case 400:
      return '请求不合法或余额不足'
    case 401:
      return '登录已过期，请重新登录'
    case 403:
      return '当前操作被拒绝（权限或条件不满足）'
    case 404:
      return '资源不存在'
    case 409:
      return '请求冲突，行情可能已变化，请刷新后重试'
    case 422:
      return '输入不合法，请检查金额与参数'
    case 429:
      return '操作过于频繁，请稍后再试'
    case 503:
      return '服务暂时不可用，请稍后再试'
    default:
      if (typeof e.message === 'string' && e.message) return e.message
      return fallback
  }
}

/** 每笔成交使用新的幂等键；重试用同一键由服务端返回原成交。 */
export function newFxIdempotencyKey(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto
  if (c?.randomUUID) return `fx-${c.randomUUID()}`
  return `fx-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

// ── 管理端本地 pair 视图（公开列表过滤 draft，创建后需本地合并） ──

/**
 * 合并「管理员写操作返回的 FxPairAdmin」与「公开 pair 列表」。
 * 后端 `GET /fx/pairs` 过滤 draft，因此刚创建的草稿只能靠本地缓存出现；
 * 公开列表存在时以公开条目为准（其共享字段更新鲜），草稿等仅本地条目保留。
 */
export function mergeFxPairs(
  adminPairs: Record<number, FxPairPublic>,
  publicPairs: FxPairPublic[],
): FxPairPublic[] {
  const byId = new Map<number, FxPairPublic>()
  for (const [id, pair] of Object.entries(adminPairs)) byId.set(Number(id), pair)
  for (const pair of publicPairs) byId.set(pair.id, pair)
  return [...byId.values()].sort((a, b) => a.id - b.id)
}

/** 不可变地把管理端写操作返回的 FxPairAdmin 写入本地缓存。 */
export function upsertFxPairAdmin(
  store: Record<number, FxPairAdmin>,
  pair: FxPairAdmin,
): Record<number, FxPairAdmin> {
  return { ...store, [pair.id]: pair }
}

// ── 玩家 API（/api/v1/fx） ──

/** Known business reasons only: never show internal exceptions to players. */
export function describeFxOrderFailure(reason: string | null): string {
  return reason && FX_ERROR_DETAILS[reason] || '到价时未能成交，请核对账户和市场状态后重新挂单'
}

export function mapFxOrderError(error: unknown, fallback: string): string {
  const value = (error ?? {}) as FxErrorLike
  const detail = typeof value.data?.detail === 'string' ? value.data.detail : ''
  return FX_ERROR_DETAILS[detail] || mapFxError({ status: value.status }, fallback)
}

export const fxApi = {
  createTriggerOrder(body: FxTriggerOrderCreate): Promise<FxTriggerOrder> {
    return api.postForCurrentUser('/api/v1/fx/orders', body)
  },
  listTriggerOrders(params: { status?: FxTriggerStatus; before_id?: number; limit?: number } = {}): Promise<FxTriggerOrder[]> {
    return api.getForCurrentUser('/api/v1/fx/orders', { params })
  },
  cancelTriggerOrder(orderId: number): Promise<FxTriggerOrder> {
    return api.postForCurrentUser(`/api/v1/fx/orders/${orderId}/cancel`, {})
  },
  quoteBorrowBuy(pairId: number, body: FxBorrowBuyQuoteRequest): Promise<FxBorrowBuyQuote> {
    return api.postForCurrentUser<FxBorrowBuyQuote>(
      `/api/v1/fx/pairs/${pairId}/borrow-buy/quote`,
      body,
    )
  },
  borrowBuy(pairId: number, body: FxBorrowBuyRequest): Promise<FxBorrowBuyTrade> {
    return api.postForCurrentUser<FxBorrowBuyTrade>(`/api/v1/fx/pairs/${pairId}/borrow-buy`, body)
  },
  getShort(pairId: number): Promise<FxShortPosition> {
    return api.get(`/api/v1/fx/pairs/${pairId}/short`)
  },
  quoteShort(pairId: number, body: FxShortQuoteRequest): Promise<FxShortQuote> {
    return api.post(`/api/v1/fx/pairs/${pairId}/short/quote`, body)
  },
  openShort(
    pairId: number,
    body: { foreign_amount: string; min_gold_out: string; idempotency_key: string },
  ): Promise<FxShortTrade> {
    return api.post(`/api/v1/fx/pairs/${pairId}/short/open`, body)
  },
  coverShort(
    pairId: number,
    body: {
      foreign_amount?: string
      cover_all?: boolean
      max_gold_in: string
      idempotency_key: string
    },
  ): Promise<FxShortTrade> {
    return api.post(`/api/v1/fx/pairs/${pairId}/short/cover`, body)
  },
  getAllMyTrades(limit = 100): Promise<FxPersonalTrade[]> {
    return api.get<FxPersonalTrade[]>('/api/v1/fx/my-trades', { params: { limit } })
  },
  listPairs(): Promise<FxPairPublic[]> {
    return api.get<FxPairPublic[]>('/api/v1/fx/pairs')
  },

  getSnapshot(pairId: number): Promise<FxSnapshot> {
    return api.get<FxSnapshot>(`/api/v1/fx/pairs/${pairId}/snapshot`)
  },

  getQuote(pairId: number, body: FxQuoteRequest): Promise<FxQuote> {
    return api.post<FxQuote>(`/api/v1/fx/pairs/${pairId}/quote`, body)
  },

  trade(pairId: number, body: FxTradeRequest): Promise<FxTradePublic> {
    return api.post<FxTradePublic>(`/api/v1/fx/pairs/${pairId}/trades`, body)
  },

  getTrades(pairId: number, limit = 50): Promise<FxTradePublic[]> {
    return api.get<FxTradePublic[]>(`/api/v1/fx/pairs/${pairId}/trades`, {
      params: { limit },
    })
  },

  /** 当前用户在指定 pair 的钱包（未交易过时后端返回零值，不 404）。 */
  getWallet(pairId: number): Promise<FxWalletPublic> {
    return api.get<FxWalletPublic>(`/api/v1/fx/pairs/${pairId}/wallet`)
  },

  /** 当前用户在指定 pair 的个人成交历史（新到旧）。 */
  getMyTrades(pairId: number, limit = 50): Promise<FxPersonalTrade[]> {
    return api.get<FxPersonalTrade[]>(`/api/v1/fx/pairs/${pairId}/my-trades`, {
      params: { limit },
    })
  },

  async getChart(
    pairId: number,
    interval: string,
    fromIso: string,
    toIso: string,
  ): Promise<FxChartPoint[]> {
    const raw = await api.get<unknown>(`/api/v1/fx/pairs/${pairId}/chart`, {
      params: { interval, from: fromIso, to: toIso },
    })
    return normalizeFxCandles(raw)
  },

  /** 同 body（旧 `getChart` 行为），额外返回 additive 响应头里的覆盖游标/版本。 */
  getChartWithMeta(
    pairId: number,
    interval: string,
    fromIso: string,
    toIso: string,
  ): Promise<FxChartMeta> {
    return getChartWithMeta(pairId, interval, fromIso, toIso)
  },
}

// ── 管理员 API（/api/v1/admin/fx，仅超管） ──

export const fxAdminApi = {
  getOverview(pairId: number): Promise<FxAdminOverview> {
    return api.get<FxAdminOverview>(`/api/v1/admin/fx/pairs/${pairId}/overview`)
  },
  previewLiquidity(pairId: number, body: FxLiquidityPreviewRequest): Promise<FxLiquidityPreview> {
    return api.post<FxLiquidityPreview>(`/api/v1/admin/fx/pairs/${pairId}/liquidity-preview`, body)
  },
  getLiquidityHistory(pairId: number, beforeId?: number): Promise<FxLiquidityHistory> {
    return api.get<FxLiquidityHistory>(`/api/v1/admin/fx/pairs/${pairId}/liquidity-history`, { params: { limit: 20, before_id: beforeId } })
  },
  /** 管理端只读列表：含草稿与 treasury 余额（超管）。 */
  listPairs(): Promise<FxPairAdminDetail[]> {
    return api.get<FxPairAdminDetail[]>('/api/v1/admin/fx/pairs')
  },

  createPair(body: FxPairCreate): Promise<FxPairAdmin> {
    return api.post<FxPairAdmin>('/api/v1/admin/fx/pairs', body)
  },

  updatePair(pairId: number, body: FxPairPatch): Promise<FxPairAdmin> {
    return api.patch<FxPairAdmin>(`/api/v1/admin/fx/pairs/${pairId}`, body)
  },

  archivePair(pairId: number): Promise<FxPairAdmin> {
    return api.post<FxPairAdmin>(`/api/v1/admin/fx/pairs/${pairId}/archive`)
  },

  deletePair(pairId: number): Promise<void> {
    return api.delete<void>(`/api/v1/admin/fx/pairs/${pairId}`)
  },

  fundPair(pairId: number, body: FxFundRequest): Promise<FxPairAdmin> {
    return api.post<FxPairAdmin>(`/api/v1/admin/fx/pairs/${pairId}/fund`, body)
  },

  withdrawPair(pairId: number, body: FxFundRequest): Promise<FxPairAdmin> {
    return api.post<FxPairAdmin>(`/api/v1/admin/fx/pairs/${pairId}/withdraw`, body)
  },

  getConfig(): Promise<Record<string, string>> {
    return api.get<Record<string, string>>('/api/v1/admin/fx/config')
  },

  setConfig(key: string, value: string): Promise<Record<string, string>> {
    return api.put<Record<string, string>>('/api/v1/admin/fx/config', { key, value })
  },
}

// ── SSE 客户端：/api/v1/fx/stream/{pair_id}（命名事件 `fx`） ──

type FxFrameListener = (frame: FxPublicFrame) => void
type FxEnvelopeListener = (envelope: FxPublicEnvelope) => void
type FxVoidListener = () => void
type FxErrorListener = (error: unknown) => void

export class FxStream {
  private source: SiteEventSource | null = null
  private pairId: number | null = null
  private reconnectAttempts = 0
  private readonly baseDelay = 1000
  private readonly maxDelay = 30000
  private gen = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null

  private readonly frameListeners = new Set<FxFrameListener>()
  private readonly envelopeListeners = new Set<FxEnvelopeListener>()
  private readonly openListeners = new Set<FxVoidListener>()
  private readonly announcementListeners = new Set<FxVoidListener>()
  private readonly errorListeners = new Set<FxErrorListener>()

  connect(pairId: number): void {
    if (this.pairId === pairId && this.source) return
    this.disconnect()
    this.reconnectAttempts = 0
    this.openConnection(pairId)
  }

  private openConnection(pairId: number): void {
    if (typeof EventSource === 'undefined') {
      this.emitError(new Error('EventSource unavailable'))
      return
    }
    this.gen += 1
    this.pairId = pairId
    const baseUrl = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8004').replace(
      /\/$/,
      '',
    )
    const url = `${baseUrl}/api/v1/fx/stream/${pairId}`
    try {
      const source = openSiteEventSource(url)
      const generation = this.gen
      this.source = source
      const current = () => this.source === source && this.gen === generation
      const receive = (event: MessageEvent) => {
        if (!current()) return
        const raw = typeof event.data === 'string' ? event.data : null
        // 一次解析同时喂两种监听器：旧 onFrame 只拿到报价白名单帧，
        // 新 onEnvelope 额外拿到历史版本/尾段/逐笔成交；私有字段都被丢弃。
        const envelope = parseFxSseEnvelope(raw)
        if (envelope) {
          this.envelopeListeners.forEach((cb) => cb(envelope))
          const frame = sanitizeFxFrame(envelope)
          if (frame) this.frameListeners.forEach((cb) => cb(frame))
          return
        }
      }
      source.addEventListener('fx', receive)
      source.addEventListener('snapshot', (event) => {
        receive(event as MessageEvent)
        // The server subscribes before sending snapshot. Read the latest
        // announcement here so entry/reconnect cannot miss the subscribe gap.
        if (current()) this.announcementListeners.forEach((cb) => cb())
      })
      source.addEventListener('fx_announcement', () => {
        if (current()) this.announcementListeners.forEach((cb) => cb())
      })
      source.onopen = () => {
        if (!current()) return
        this.reconnectAttempts = 0
        this.openListeners.forEach((cb) => cb())
      }
      source.onerror = (error) => {
        if (!current()) return
        this.emitError(error)
        this.scheduleReconnect(pairId)
      }
    } catch (error) {
      this.emitError(error)
      this.scheduleReconnect(pairId)
    }
  }

  private scheduleReconnect(pairId: number): void {
    if (this.source) {
      this.source.close()
      this.source = null
    }
    if (this.pairId === null) return
    const gen = this.gen
    this.reconnectAttempts += 1
    const base = Math.min(this.baseDelay * 2 ** (this.reconnectAttempts - 1), this.maxDelay)
    const delay = base * (0.7 + Math.random() * 0.6)
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    this.reconnectTimer = setTimeout(() => {
      if (gen !== this.gen || this.pairId !== pairId) return
      this.openConnection(pairId)
    }, delay)
  }

  private emitError(error: unknown): void {
    this.errorListeners.forEach((cb) => cb(error))
  }

  onFrame(cb: FxFrameListener): void {
    this.frameListeners.add(cb)
  }

  offFrame(cb: FxFrameListener): void {
    this.frameListeners.delete(cb)
  }

  /** 订阅完整公开信封（含历史版本/尾段/逐笔成交）；旧 onFrame 行为保持不变。 */
  onEnvelope(cb: FxEnvelopeListener): void {
    this.envelopeListeners.add(cb)
  }

  offEnvelope(cb: FxEnvelopeListener): void {
    this.envelopeListeners.delete(cb)
  }

  onOpen(cb: FxVoidListener): void {
    this.openListeners.add(cb)
  }

  offOpen(cb: FxVoidListener): void {
    this.openListeners.delete(cb)
  }

  onAnnouncementChanged(cb: FxVoidListener): void {
    this.announcementListeners.add(cb)
  }

  offAnnouncementChanged(cb: FxVoidListener): void {
    this.announcementListeners.delete(cb)
  }

  onError(cb: FxErrorListener): void {
    this.errorListeners.add(cb)
  }

  offError(cb: FxErrorListener): void {
    this.errorListeners.delete(cb)
  }

  reconnectNow(): void {
    if (this.pairId === null || this.source !== null) return
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
    this.reconnectAttempts = 0
    this.openConnection(this.pairId)
  }

  disconnect(): void {
    this.gen += 1
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    if (this.source) {
      this.source.close()
      this.source = null
    }
    this.pairId = null
    this.reconnectAttempts = 0
  }

  get isConnected(): boolean {
    return this.source !== null && this.source.readyState === EventSource.OPEN
  }

  get currentPairId(): number | null {
    return this.pairId
  }
}

/** Exact decimal ceiling prevents a cover limit from rounding below the chosen tolerance. */
export function computeMaxGoldIn(input: string, slippageBps: number): string {
  const match = /^(\d+)(?:\.(\d+))?$/.exec(input.trim())
  if (!match) return ''
  const fraction = match[2] ?? ''
  const denominator = 10n ** BigInt(fraction.length) * 10000n
  const numerator = BigInt(match[1]! + fraction) * BigInt(10000 + clampBps(slippageBps)) * 1000000n
  return scaledToString((numerator + denominator - 1n) / denominator, 6)
}

export interface FxPendingShortRequest {
  readonly pairId: number
  readonly action: 'open' | 'cover'
  readonly body: Readonly<{
    foreign_amount?: string
    cover_all?: boolean
    min_gold_out?: string
    max_gold_in?: string
    idempotency_key: string
  }>
}

function restoredShortRequest(value: unknown): FxPendingShortRequest | null {
  if (!value || typeof value !== 'object') return null
  const request = value as Partial<FxPendingShortRequest>
  const body = request.body
  if (
    !Number.isSafeInteger(request.pairId) ||
    Number(request.pairId) <= 0 ||
    (request.action !== 'open' && request.action !== 'cover') ||
    !body ||
    typeof body !== 'object' ||
    typeof body.idempotency_key !== 'string' ||
    !body.idempotency_key ||
    body.idempotency_key.length > 128
  )
    return null
  const amount = (v: unknown) => typeof v === 'string' && /^\d+(?:\.\d{0,6})?$/.test(v)
  if (request.action === 'open') {
    if (!amount(body.foreign_amount) || !amount(body.min_gold_out)) return null
  } else if (
    !amount(body.max_gold_in) ||
    (body.cover_all !== true && !amount(body.foreign_amount)) ||
    (body.cover_all === true && body.foreign_amount !== undefined)
  )
    return null
  return Object.freeze({
    pairId: request.pairId!,
    action: request.action,
    body: Object.freeze({ ...body }),
  })
}

/** Wire-specific adapter; old short requests retain their route and identity. */
export class FxPendingShortOrder extends FxPendingOrder<FxPendingShortRequest> {
  constructor(userId: number | string | null = null, storage?: FxPendingStorage | null) {
    super(userId, 'fx-short-pending-v1:', restoredShortRequest, storage)
  }
  start<T>(
    pairId: number,
    action: 'open' | 'cover',
    body: Omit<FxPendingShortRequest['body'], 'idempotency_key'>,
    run: (request: FxPendingShortRequest) => Promise<T>,
  ): Promise<T | null> {
    return this.submit({ pairId, action,
      body: { ...body, idempotency_key: newFxIdempotencyKey() } }, run)
  }
}
