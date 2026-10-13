// FX K 线增量内核（纯函数 + 纯类，不依赖 lightweight-charts / DOM）。
//
// 复用市场页 `CandleChart.vue` 已验证的成熟行为：
//   - 后端 `fill=true` 已把空桶填到当前 bucket，因此初始加载的最后一根永远当作
//     forming candle 原地更新，不用客户端时钟判断（避免时钟漂移归类错误）；
//   - 价格事件跨桶时本地合成空桶（o=h=l=c=prevClose, v=0），跨桶过多才要求整页重载；
//   - 同一 bucket 合并 h/l/c/v；MA 增量维护，避免逐笔 O(N) 重算。
//
// SSE events 使用事件时间和前后边际价。
//
// 放到 `utils/` 下让 Vitest（node 环境）可直接对 bucket/MA/实时合并做行为断言。

import type { FxChartPoint } from '@/types/fx'

export interface FxCandle {
  /** bucket 起点（秒） */
  t: number
  o: number
  h: number
  l: number
  c: number
  v: number
  n_events?: number
  n_trades?: number
}

/** 图表库无法把空时间轴映射为可视区间；首笔成交前保持空态。 */
export function fxCandleVisibleRange(
  candles: readonly FxCandle[], nowSeconds: number, stepSeconds: number,
): { from: number; to: number } | null {
  const last = candles[candles.length - 1]
  if (!last) return null
  const to = Math.max(nowSeconds, last.t + stepSeconds)
  return { from: to - Math.max(60, 80 * stepSeconds), to }
}

/** lightweight-charts 可视时间端点取真实桶，不能用墙钟差判断用户回看。 */
export function fxCandleScrolledBack(candles: readonly FxCandle[], visibleTo: number): boolean {
  const last = candles[candles.length - 1]
  return !!last && visibleTo < last.t
}

/**
 * 已提交公开价格事件（wire → 数值化后的引擎输入）。
 * - `ts` 是事件时间（epoch 秒），不是客户端接收时间；
 * - `volume` 是金侧成交量（buy 金入 / sell 金出），引擎只做去重后的累加；
 * - `id` 是 FxPoolEvent.id，用于跨帧/补尾去重。
 */
export interface FxCandleEvent {
  id: number
  ts: number
  prePrice: number
  price: number
  volume: number
  nTrades?: number
}

export interface FxEventsApply {
  /** 桶缺口过大、落在已知区间之外，或预加载桶出现无法判定的乱序成交 → 重读历史 */
  reload: boolean
  /** 本次被创建或修改的 candle，按时间升序去重 */
  changed: FxCandle[]
  /** 新增 candle 数（含补的中间空桶） */
  added: number
  /** 实际计入的事件数 */
  applied: number
  /** 被跳过的事件数：非法 / 重复 id / <= 尾段覆盖游标 */
  skipped: number
  /** 引擎已计入的最大事件 id；无则 null */
  throughEventId: number | null
}

/** 桶内排序键：与后端 `(created_at, id)` 口径一致（先 ts 后 id）。 */
export interface FxEventKey {
  ts: number
  id: number
}

/** 比较两个桶内排序键；<0 表示 a 更早。 */
export function compareFxEventKeys(a: FxEventKey, b: FxEventKey): number {
  return a.ts === b.ts ? a.id - b.id : a.ts - b.ts
}

/** 本地合成空桶的上限，超过则回退整页重载（与市场页同量级）。 */
export const FX_MAX_LOCAL_FILL_BUCKETS = 360

export const FX_MA_PERIODS = [10, 20] as const
export const FX_DEFAULT_MA_PERIOD = 10

/** 对齐到 bucket 起点。非法 step 返回 ts 本身。 */
export function fxBucketStart(tsSeconds: number, stepSeconds: number): number {
  if (!Number.isFinite(tsSeconds) || !Number.isFinite(stepSeconds) || stepSeconds <= 0) {
    return tsSeconds
  }
  return Math.floor(tsSeconds / stepSeconds) * stepSeconds
}

/** 把 ISO 时间解析为秒；非法返回 null。 */
export function fxTimestampToSeconds(iso: string): number | null {
  const ms = new Date(iso).getTime()
  if (!Number.isFinite(ms)) return null
  return Math.floor(ms / 1000)
}

/** 归一化 `/chart` 点：丢弃非法时间/价格，按时间升序去重（同 t 保留最后一条）。 */
export function fxChartPointsToCandles(points: readonly FxChartPoint[]): FxCandle[] {
  const byTime = new Map<number, FxCandle>()
  for (const point of points) {
    const t = fxTimestampToSeconds(point.t)
    if (t === null) continue
    const o = Number(point.o)
    const h = Number(point.h)
    const l = Number(point.l)
    const c = Number(point.c)
    const v = Number(point.v)
    if (!Number.isFinite(o) || !Number.isFinite(h) || !Number.isFinite(l) || !Number.isFinite(c)) {
      continue
    }
    byTime.set(t, { t, o, h, l, c, v: Number.isFinite(v) ? v : 0, n_events: point.n_events, n_trades: point.n_trades })
  }
  return [...byTime.values()].sort((a, b) => a.t - b.t)
}

/**
 * 简单移动平均（按收盘价）。返回与 candles 同序的数组，前 `period - 1` 位为 NaN。
 */
export function fxMovingAverage(candles: readonly FxCandle[], period: number): number[] {
  const out = new Array<number>(candles.length).fill(Number.NaN)
  const p = Math.max(1, Math.floor(period))
  let sum = 0
  for (let i = 0; i < candles.length; i++) {
    sum += candles[i]!.c
    if (i >= p) sum -= candles[i - p]!.c
    if (i >= p - 1) out[i] = sum / p
  }
  return out
}

/**
 * FX K 线增量引擎。组件只负责把 `changed` 推给 lightweight-charts，
 * 业务归类/空桶合成/MA 全在这里，便于单测。
 */
export class FxCandleEngine {
  private stepSeconds: number
  private readonly maPeriods: number[]
  private _candles: FxCandle[] = []
  private readonly maCache = new Map<number, number[]>()
  /** 已计入的实时事件 id（跨帧去重），随 coverage 提升裁剪 */
  private readonly seenEventIds = new Set<number>()
  /** SSE 尾段已覆盖的事件 id：<= 此值的成交已在历史里，必须跳过 */
  private _coverageEventId: number | null = null
  /** 已计入的最大实时事件 id */
  private _throughEventId: number | null = null
  /**
   * 实时成交桶的 (ts,id) 首末排序键。仅对“本引擎创建/回填”的桶有完整 first；
   * 从 `load` 预加载的桶只有在被实时成交续写后才记录（first=null 表示历史首笔未知）。
   */
  private readonly eventRanges = new Map<number, { first: FxEventKey | null; last: FxEventKey }>()
  /** `load` 时最后一个预加载桶的 epoch；用于区分“预加载桶”与实时创建桶。 */
  private preloadedThroughEpoch: number | null = null

  constructor(stepSeconds = 60, maPeriods: readonly number[] = FX_MA_PERIODS) {
    this.stepSeconds = FX_CANDLE_STEP(stepSeconds)
    this.maPeriods = maPeriods.length > 0 ? [...new Set(maPeriods)] : [FX_DEFAULT_MA_PERIOD]
  }

  get step(): number {
    return this.stepSeconds
  }

  get candles(): readonly FxCandle[] {
    return this._candles
  }

  get count(): number {
    return this._candles.length
  }

  /** SSE 尾段覆盖到的最后事件 id；null 表示未知（退化为纯 id 去重）。 */
  get coverageEventId(): number | null {
    return this._coverageEventId
  }

  /** 已计入的最大实时事件 id。 */
  get throughEventId(): number | null {
    return this._throughEventId
  }

  /** 切换周期：步长变化时清空（调用方随后会重新 load）。 */
  setStep(stepSeconds: number): void {
    const next = FX_CANDLE_STEP(stepSeconds)
    if (next === this.stepSeconds) return
    this.stepSeconds = next
    this.clear()
  }

  /**
   * 用整段历史重置（初始加载 / 切周期 / gap reload）。
   * `coverageEventId` 来自 `history_tail_through_event_id`：<= 该 id 的实时帧
   * 已经在历史里，`applyEvents` 会跳过，避免成交量重复累计。
   */
  load(points: readonly FxChartPoint[], coverageEventId: number | null = null): void {
    this._candles = fxChartPointsToCandles(points)
    this.seenEventIds.clear()
    this.eventRanges.clear()
    this._throughEventId = null
    this.preloadedThroughEpoch = this._candles.length > 0
      ? this._candles[this._candles.length - 1]!.t : null
    this.setCoverageEventId(coverageEventId)
    this.rebuildMa(0)
  }

  /** 记录尾段覆盖游标；同时裁剪 <= 该 id 的去重集合，避免无界增长。 */
  setCoverageEventId(id: number | null): void {
    this._coverageEventId = Number.isSafeInteger(id) && (id as number) >= 0 ? id : null
    if (this._coverageEventId === null) return
    for (const seen of this.seenEventIds) {
      if (seen <= this._coverageEventId) this.seenEventIds.delete(seen)
    }
  }

  clear(): void {
    this._candles = []
    this.maCache.clear()
    this.seenEventIds.clear()
    this.eventRanges.clear()
    this._coverageEventId = null
    this._throughEventId = null
    this.preloadedThroughEpoch = null
  }

  /** 与 candles 同序的 MA 序列（前 period-1 位为 NaN）。 */
  ma(period: number): readonly number[] {
    if (!this.maCache.has(period)) this.rebuildMa(0)
    return this.maCache.get(period) ?? []
  }

  maAt(period: number, index: number): number | null {
    const values = this.ma(period)
    const value = values[index]
    return value === undefined || Number.isNaN(value) ? null : value
  }

  /**
   * 批量应用真实成交（含金侧成交量与真实成交时间）：
   * - 按 `ts`（真实成交时间）分桶；O 为首笔成交前价，C 为末笔成交后价，
   *   H/L 包含每笔成交前后价，与历史聚合一致；prevClose 只用于空桶；
   * - `id <= coverageEventId` 或重复 id 的成交被跳过，保证成交量不重复累计；
   * - 桶内按 `(ts,id)` 维护首末排序键：实时创建的桶可跨批次正确回填 open/close；
   * - 预加载（`load`）的桶没有历史首末键：只有“当前 forming 桶的追加”可安全应用，
   *   更早预加载桶出现乱序成交时无法判定时序 → `reload`，绝不猜 O/C；
   * - 整批先预检，缺口/乱序命中则 `reload: true` 且不改动任何 candle；
   * - MA 只在批末重建一次。
   */
  applyEvents(events: readonly FxCandleEvent[]): FxEventsApply {
    const result: FxEventsApply = {
      reload: false, changed: [], added: 0, applied: 0, skipped: 0,
      throughEventId: this._throughEventId,
    }
    if (events.length === 0) return result

    const accepted: FxCandleEvent[] = []
    const batchIds = new Set<number>()
    for (const event of events) {
      if (!isValidEvent(event)) {
        result.skipped += 1
        continue
      }
      if (this._coverageEventId !== null && event.id <= this._coverageEventId) {
        result.skipped += 1
        continue
      }
      if (this.seenEventIds.has(event.id) || batchIds.has(event.id)) {
        result.skipped += 1
        continue
      }
      batchIds.add(event.id)
      accepted.push(event)
    }
    if (accepted.length === 0) return result

    const sorted = [...accepted].sort((a, b) => a.ts - b.ts || a.id - b.id)

    // ── 预检（不改动 candle）：整批要么全部可应用，要么 reload ──
    const firstT = this._candles[0]?.t
    const originalLastT = this._candles[this._candles.length - 1]?.t
    const simRanges = new Map<number, { first: FxEventKey | null; last: FxEventKey }>()
    this.eventRanges.forEach((range, epoch) => simRanges.set(epoch, { first: range.first, last: range.last }))
    let simLastT = originalLastT
    let maxBucket = originalLastT

    for (const event of sorted) {
      const bucket = fxBucketStart(Math.floor(event.ts), this.stepSeconds)
      const key: FxEventKey = { ts: event.ts, id: event.id }
      if (firstT !== undefined && bucket < firstT) return { ...result, reload: true }

      if (simLastT === undefined || bucket > simLastT) {
        simRanges.set(bucket, { first: key, last: key })
        simLastT = bucket
        maxBucket = bucket
        continue
      }

      const exists = originalLastT !== undefined && bucket <= originalLastT
        ? findCandleIndex(this._candles, bucket) >= 0
        : true
      if (!exists) return { ...result, reload: true }   // 已知区间内的空桶缺口

      const preloaded = this.preloadedThroughEpoch !== null && bucket <= this.preloadedThroughEpoch
      const range = simRanges.get(bucket)
      if (!range) {
        // 只有明确 n_events=0 的桶可重置；注撤资的零量桶仍保留历史 OHLC。
        const index = findCandleIndex(this._candles, bucket)
        if (index >= 0 && this._candles[index]!.n_events === undefined) return { ...result, reload: true }
        const syntheticEmpty = index >= 0 && this._candles[index]!.n_events === 0
        if (preloaded && !syntheticEmpty && bucket !== this.preloadedThroughEpoch) {
          // 更早的预加载真实桶：无法判定乱序成交的时序
          return { ...result, reload: true }
        }
        simRanges.set(
          bucket,
          syntheticEmpty ? { first: key, last: key } : { first: null, last: key },
        )
      } else if (range.first === null) {
        if (compareFxEventKeys(key, range.last) < 0) return { ...result, reload: true }
        range.last = key
      } else if (compareFxEventKeys(key, range.first) < 0) {
        range.first = key
      } else if (compareFxEventKeys(key, range.last) > 0) {
        range.last = key
      }
    }
    if (originalLastT !== undefined && maxBucket !== undefined
      && maxBucket - originalLastT > (FX_MAX_LOCAL_FILL_BUCKETS + 1) * this.stepSeconds) {
      return { ...result, reload: true }
    }

    // ── 应用（预检已保证可行） ──
    const changedIndexes = new Set<number>()
    for (const event of sorted) {
      const bucket = fxBucketStart(Math.floor(event.ts), this.stepSeconds)
      const key: FxEventKey = { ts: event.ts, id: event.id }
      const candles = this._candles
      if (candles.length === 0) {
        candles.push({ t: bucket, o: event.prePrice, h: Math.max(event.prePrice, event.price), l: Math.min(event.prePrice, event.price), c: event.price, v: event.volume, n_events: 1, n_trades: event.nTrades ?? 1 })
        this.eventRanges.set(bucket, { first: key, last: key })
        changedIndexes.add(0)
        result.added += 1
      } else {
        const currentLast = candles[candles.length - 1]!
        if (bucket > currentLast.t) {
          const prevClose = currentLast.c
          for (let t = currentLast.t + this.stepSeconds; t < bucket; t += this.stepSeconds) {
            candles.push({ t, o: prevClose, h: prevClose, l: prevClose, c: prevClose, v: 0, n_events: 0, n_trades: 0 })
            changedIndexes.add(candles.length - 1)
            result.added += 1
          }
          // 新桶以事件前价开盘；不把额外池价变化伪装成前收盘。
          candles.push({ t: bucket, o: event.prePrice, h: Math.max(event.prePrice, event.price), l: Math.min(event.prePrice, event.price), c: event.price, v: event.volume, n_events: 1, n_trades: event.nTrades ?? 1 })
          this.eventRanges.set(bucket, { first: key, last: key })
          changedIndexes.add(candles.length - 1)
          result.added += 1
        } else {
          const index = findCandleIndex(candles, bucket)
          if (index < 0) {
            // 预检已排除；保留防御性 reload（不伪造、不猜时序）。
            return { ...result, reload: true, changed: [], added: 0 }
          }
          const candle = candles[index]!
          const range = this.eventRanges.get(bucket)
          if (!range) {
            if (candle.n_events === 0) {
              // 明确合成空桶：首个真实事件定义整根
              // O/H/L/C，不保留空桶 carry 的旧价格/极值。
              candle.o = event.prePrice
              candle.h = Math.max(event.prePrice, event.price)
              candle.l = Math.min(event.prePrice, event.price)
              candle.c = event.price
              candle.v = event.volume
              this.eventRanges.set(bucket, { first: key, last: key })
            } else {
              // 预加载真实 forming 桶：历史首笔未知，实时成交只能追加到 close
              candle.h = Math.max(candle.h, event.prePrice, event.price)
              candle.l = Math.min(candle.l, event.prePrice, event.price)
              candle.c = event.price
              candle.v += event.volume
              this.eventRanges.set(bucket, { first: null, last: key })
            }
          } else if (range.first === null) {
            candle.h = Math.max(candle.h, event.prePrice, event.price)
            candle.l = Math.min(candle.l, event.prePrice, event.price)
            candle.c = event.price
            candle.v += event.volume
            range.last = key
          } else {
            if (compareFxEventKeys(key, range.first) < 0) {
              candle.o = event.prePrice
              range.first = key
            } else if (compareFxEventKeys(key, range.last) > 0) {
              candle.c = event.price
              range.last = key
            }
            candle.h = Math.max(candle.h, event.prePrice, event.price)
            candle.l = Math.min(candle.l, event.prePrice, event.price)
            candle.v += event.volume
          }
          candle.n_events = (candle.n_events ?? 0) + 1
          candle.n_trades = (candle.n_trades ?? 0) + (event.nTrades ?? 1)
          changedIndexes.add(index)
          // 延迟事件更新收盘后，已补出的水平桶也须携带新收盘。
          for (let i = index + 1; i < candles.length && candles[i]!.n_events === 0; i++) {
            const carry = candles[i]!
            carry.o = carry.h = carry.l = carry.c = candle.c
            changedIndexes.add(i)
          }
        }
      }
      this.seenEventIds.add(event.id)
      this._throughEventId = this._throughEventId === null
        ? event.id : Math.max(this._throughEventId, event.id)
      result.applied += 1
    }

    if (changedIndexes.size > 0) {
      const indexes = [...changedIndexes].sort((a, b) => a - b)
      this.rebuildMa(indexes[0]!)
      result.changed = indexes.map((index) => this._candles[index]!)
    }
    result.throughEventId = this._throughEventId
    return result
  }

  /** Quiet time extends the last covered close, without inventing source events. */
  carryTo(tsSeconds: number): Pick<FxEventsApply, 'reload' | 'changed' | 'added'> {
    const last = this._candles[this._candles.length - 1]
    if (!last || last.n_events === undefined || !Number.isFinite(tsSeconds)) {
      return { reload: false, changed: [], added: 0 }
    }
    const bucket = fxBucketStart(tsSeconds, this.stepSeconds)
    const count = (bucket - last.t) / this.stepSeconds
    if (count > FX_MAX_LOCAL_FILL_BUCKETS) return { reload: true, changed: [], added: 0 }
    const changed: FxCandle[] = []
    for (let t = last.t + this.stepSeconds; t <= bucket; t += this.stepSeconds) {
      const candle: FxCandle = { t, o: last.c, h: last.c, l: last.c, c: last.c, v: 0, n_events: 0, n_trades: 0 }
      this._candles.push(candle)
      changed.push(candle)
    }
    if (changed.length) this.rebuildMa(this._candles.length - changed.length)
    return { reload: false, changed, added: changed.length }
  }

  private rebuildMa(from: number): void {
    const length = this._candles.length
    for (const period of this.maPeriods) {
      let values = this.maCache.get(period)
      if (!values || values.length > length) {
        values = new Array<number>(length).fill(Number.NaN)
        this.maCache.set(period, values)
      } else {
        while (values.length < length) values.push(Number.NaN)
      }
      const start = Math.max(period - 1, Math.max(0, from))
      for (let i = start; i < length; i++) {
        let sum = 0
        for (let j = i - period + 1; j <= i; j++) sum += this._candles[j]!.c
        values[i] = sum / period
      }
    }
  }
}

function FX_CANDLE_STEP(stepSeconds: number): number {
  return Number.isFinite(stepSeconds) && stepSeconds > 0 ? Math.floor(stepSeconds) : 60
}

/** 池子事件输入合法性：id 安全非负、ts 有限正、量化价格及量有限非负。 */
function isValidEvent(event: FxCandleEvent): boolean {
  return Number.isSafeInteger(event.id) && event.id >= 0
    && Number.isFinite(event.ts) && event.ts > 0
    && Number.isFinite(event.prePrice) && event.prePrice >= 0
    && Number.isFinite(event.price) && event.price >= 0
    && Number.isFinite(event.volume) && event.volume >= 0
}

/** 在按 t 升序的 candle 数组里二分查找 bucket，返回下标；不存在返回 -1。 */
function findCandleIndex(candles: readonly FxCandle[], bucket: number): number {
  let low = 0
  let high = candles.length - 1
  while (low <= high) {
    const mid = (low + high) >> 1
    const value = candles[mid]!.t
    if (value === bucket) return mid
    if (value < bucket) low = mid + 1
    else high = mid - 1
  }
  return -1
}
