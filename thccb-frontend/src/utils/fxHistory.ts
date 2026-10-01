// FX 缓存历史的纯工具层：SSE/`/history/fx/` 列式段的严格校验、解码与合并。
//
// 与 LMSR `api/history.ts` 的关键差异：
//   - FX 价格/成交量的历史编码是十进制字符串，**不套用 price × 1e8 定点整数**；
//     只有本文件 `decodeFxHistorySegment`（图表边界）才把字符串转 number。
//   - FX 段按 interval 区分，封存段长与后端 `RING_SPEC.segment` 一致。
//
// 这里是纯函数，不依赖 DOM / lightweight-charts / axios，便于 Vitest(node) 直接断言：
// 段结构非法要整段丢弃，而不是用空桶冒充“完整成交量”。

import type {
  FxChartPoint,
  FxHistoryInterval,
  FxHistorySegment,
  FxHistoryTail,
  FxTradeTick,
} from '@/types/fx'
import { fxTimestampToSeconds, type FxCandleTrade } from '@/utils/fxCandle'

/** 与后端 RING_SPEC 一致的四档周期。 */
export const FX_HISTORY_INTERVALS: readonly FxHistoryInterval[] = ['10s', '1m', '15m', '1h']

/** 桶宽（秒），与后端 `RING_SPEC[interval].step` 一致。 */
export const FX_HISTORY_INTERVAL_SECONDS: Record<FxHistoryInterval, number> = {
  '10s': 10,
  '1m': 60,
  '15m': 900,
  '1h': 3600,
}

/** 封存段长（秒），与后端 `RING_SPEC[interval].segment` 一致。 */
export const FX_HISTORY_SEGMENT_SECONDS: Record<FxHistoryInterval, number> = {
  '10s': 600,
  '1m': 3600,
  '15m': 86400,
  '1h': 604800,
}

/** `/history/fx/` 单次 lookback 允许请求的最大封存段数，防止异常区间拉爆请求。 */
export const FX_HISTORY_MAX_SEGMENTS = 512

const DECIMAL_PATTERN = /^-?\d+(?:\.\d+)?$/

export function isFxHistoryInterval(value: unknown): value is FxHistoryInterval {
  return typeof value === 'string' && (FX_HISTORY_INTERVALS as readonly string[]).includes(value)
}

/** 十进制字符串 → 有限 number；非法返回 null。仅图表边界使用。 */
function decimalToNumber(value: unknown): number | null {
  if (typeof value !== 'string' || !DECIMAL_PATTERN.test(value.trim())) return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function safeNonNegativeInt(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null
}

/**
 * 严格校验一个列式段：对象、数值边界有限、所有列等长、偏移在 n_buckets 内、
 * 价格/量非负。任一不满足返回 null（调用方整段丢弃或回退 `/chart`）。
 */
export function sanitizeFxHistorySegment(raw: unknown): FxHistorySegment | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null
  const source = raw as Record<string, unknown>
  const t0 = safeNonNegativeInt(source.t0)
  const step = safeNonNegativeInt(source.step)
  const nBuckets = safeNonNegativeInt(source.n_buckets)
  if (t0 === null || step === null || step <= 0 || nBuckets === null || nBuckets <= 0) return null

  const columns = ['t', 'o', 'h', 'l', 'c', 'v', 'trades'] as const
  const rawColumns: Record<(typeof columns)[number], unknown[]> = {
    t: [], o: [], h: [], l: [], c: [], v: [], trades: [],
  }
  let length = -1
  for (const key of columns) {
    const column = source[key]
    if (!Array.isArray(column)) return null
    if (length === -1) length = column.length
    else if (column.length !== length) return null
    rawColumns[key] = column
  }
  if (length === 0) return { t0, step, n_buckets: nBuckets, t: [], o: [], h: [], l: [], c: [], v: [], trades: [] }

  const t: number[] = []
  const o: string[] = []
  const h: string[] = []
  const l: string[] = []
  const c: string[] = []
  const v: string[] = []
  const trades: number[] = []
  for (let i = 0; i < length; i++) {
    const offset = rawColumns.t[i]
    if (typeof offset !== 'number' || !Number.isSafeInteger(offset) || offset < 0 || offset >= nBuckets) return null
    const open = rawColumns.o[i]
    const high = rawColumns.h[i]
    const low = rawColumns.l[i]
    const close = rawColumns.c[i]
    const volume = rawColumns.v[i]
    const count = rawColumns.trades[i]
    for (const price of [open, high, low, close]) {
      const parsed = decimalToNumber(price)
      if (parsed === null || parsed < 0) return null
    }
    const parsedVolume = decimalToNumber(volume)
    const parsedCount = safeNonNegativeInt(count)
    if (parsedVolume === null || parsedVolume < 0 || parsedCount === null) return null
    t.push(offset)
    o.push(open as string)
    h.push(high as string)
    l.push(low as string)
    c.push(close as string)
    v.push(volume as string)
    trades.push(parsedCount)
  }
  return { t0, step, n_buckets: nBuckets, t, o, h, l, c, v, trades }
}

/** 校验 SSE `history_tail`：只保留已知 interval 且结构合法的段。 */
export function sanitizeFxHistoryTail(raw: unknown): FxHistoryTail | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null
  const source = raw as Record<string, unknown>
  const tail: FxHistoryTail = {}
  for (const interval of FX_HISTORY_INTERVALS) {
    const segment = sanitizeFxHistorySegment(source[interval])
    if (segment) tail[interval] = segment
  }
  return Object.keys(tail).length > 0 ? tail : null
}

/** 校验一条公开逐笔成交；字段保持字符串语义，图表边界才转 number。 */
export function sanitizeFxTradeTick(raw: unknown): FxTradeTick | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null
  const source = raw as Record<string, unknown>
  const id = safeNonNegativeInt(source.id)
  if (id === null) return null
  const ts = source.ts
  if (typeof ts !== 'string' || fxTimestampToSeconds(ts) === null) return null
  const postPrice = decimalToNumber(source.post_price)
  const goldVolume = decimalToNumber(source.gold_volume)
  if (postPrice === null || postPrice <= 0 || goldVolume === null || goldVolume < 0) return null
  return { id, ts, post_price: source.post_price as string, gold_volume: source.gold_volume as string }
}

/** 校验增量帧里的公开成交列表；非法项直接丢弃。 */
export function sanitizeFxTradeTicks(raw: unknown): FxTradeTick[] {
  if (!Array.isArray(raw)) return []
  const out: FxTradeTick[] = []
  for (const item of raw) {
    const tick = sanitizeFxTradeTick(item)
    if (tick) out.push(tick)
  }
  return out
}

/**
 * 列式段 → 图表点。价格/成交量只在**这里**转 number（图表边界），
 * 时间用 `t0 + t[i] * step` 还原；非法行不会出现（上游已校验）。
 */
export function decodeFxHistorySegment(segment: FxHistorySegment): FxChartPoint[] {
  const points: FxChartPoint[] = []
  for (let i = 0; i < segment.t.length; i++) {
    const epoch = segment.t0 + segment.t[i]! * segment.step
    points.push({
      t: new Date(epoch * 1000).toISOString(),
      o: Number(segment.o[i]),
      h: Number(segment.h[i]),
      l: Number(segment.l[i]),
      c: Number(segment.c[i]),
      v: Number(segment.v[i]),
    })
  }
  return points
}

/** 公开逐笔成交 → K 线引擎输入；非法/超出安全范围返回 null。 */
export function decodeFxTradeTick(tick: FxTradeTick): FxCandleTrade | null {
  const ts = fxTimestampToSeconds(tick.ts)
  if (ts === null) return null
  const price = Number(tick.post_price)
  const volume = Number(tick.gold_volume)
  if (!Number.isFinite(price) || price <= 0 || !Number.isFinite(volume) || volume < 0) return null
  return { id: tick.id, ts, price, volume }
}

/**
 * 覆盖 [fromSec, 最后封存边界) 的段起点列表（对齐段长；进行中的段不含）。
 * 与 LMSR `sealedSegmentEpochs` 同语义，但按 FX 的 interval 取段长。
 */
export function fxHistorySegmentEpochs(
  interval: FxHistoryInterval,
  fromSec: number,
  nowSec: number,
): number[] {
  const segment = FX_HISTORY_SEGMENT_SECONDS[interval]
  const boundary = nowSec - (nowSec % segment)
  const epochs: number[] = []
  let current = fromSec - (fromSec % segment)
  for (; current < boundary; current += segment) {
    epochs.push(current)
    if (epochs.length >= FX_HISTORY_MAX_SEGMENTS) break
  }
  return epochs
}

/**
 * 合并封存段与尾段并补齐空桶。同 bucket 时**封存段优先**（不可变、权威），
 * 避免封存边界与尾段重叠时把同一桶的成交量算两遍。
 * 缺桶用 prev_close 平推（v=0），与后端 `fill=true` 语义一致；完全无数据返回空。
 */
export function mergeFxHistoryCandles(
  sealed: readonly FxChartPoint[],
  tail: readonly FxChartPoint[],
  stepSec: number,
  fromSec: number,
  toSecExclusive: number,
): FxChartPoint[] {
  if (!Number.isFinite(stepSec) || stepSec <= 0) return []
  const byEpoch = new Map<number, FxChartPoint>()
  for (const point of tail) {
    const epoch = fxTimestampToSeconds(point.t)
    if (epoch !== null) byEpoch.set(epoch, point)
  }
  for (const point of sealed) {
    const epoch = fxTimestampToSeconds(point.t)
    if (epoch !== null) byEpoch.set(epoch, point)
  }
  const first = [...byEpoch.values()].sort(
    (a, b) => (fxTimestampToSeconds(a.t) ?? 0) - (fxTimestampToSeconds(b.t) ?? 0),
  )[0]
  if (!first) return []

  const start = fromSec - (fromSec % stepSec)
  const out: FxChartPoint[] = []
  let prevClose = first.o
  for (let epoch = start; epoch < toSecExclusive; epoch += stepSec) {
    const existing = byEpoch.get(epoch)
    if (existing) {
      out.push(existing)
      prevClose = existing.c
    } else {
      out.push({ t: new Date(epoch * 1000).toISOString(), o: prevClose, h: prevClose, l: prevClose, c: prevClose, v: 0 })
    }
  }
  return out
}
