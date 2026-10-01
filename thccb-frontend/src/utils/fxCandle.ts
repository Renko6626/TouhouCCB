// FX K 线增量内核（纯函数 + 纯类，不依赖 lightweight-charts / DOM）。
//
// 复用市场页 `CandleChart.vue` 已验证的成熟行为：
//   - 后端 `fill=true` 已把空桶填到当前 bucket，因此初始加载的最后一根永远当作
//     forming candle 原地更新，不用客户端时钟判断（避免时钟漂移归类错误）；
//   - 实时 tick 跨桶时本地合成空桶（o=h=l=c=prevClose, v=0），跨桶过多才要求整页重载；
//   - 同一 bucket 合并 h/l/c/v；MA 增量维护，避免每 tick O(N) 重算。
//
// 与市场页的唯一语义差异：FX 的 SSE 帧是「当前价格」而不是带成交量的逐笔成交，
// 所以乱序/落后 bucket 的 tick 也更新最后一根（视为客户端时钟略慢），而不是丢弃。
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
}

export interface FxPriceApply {
  /** 跨桶过多，组件应重新拉取整段历史 */
  reload: boolean
  /** 本次被创建或修改的 candle，按时间升序（最后一个是 forming candle） */
  changed: FxCandle[]
  /** 新增 candle 数（用于空态/可见数量统计） */
  added: number
}

/**
 * 已提交真实成交（wire → 数值化后的引擎输入）。
 * - `ts` 是真实成交时间（epoch 秒），不是客户端接收时间；
 * - `volume` 是金侧成交量（buy 金入 / sell 金出），引擎只做去重后的累加；
 * - `id` 是 FxTrade.id，用于跨帧/补尾去重。
 */
export interface FxCandleTrade {
  id: number
  ts: number
  price: number
  volume: number
}

export interface FxTradesApply {
  /** 桶缺口过大或落在已知区间之外，组件应重读历史（绝不伪造空桶冒充完整量） */
  reload: boolean
  /** 本次被创建或修改的 candle，按时间升序去重 */
  changed: FxCandle[]
  /** 新增 candle 数（含补的中间空桶） */
  added: number
  /** 实际计入的成交笔数 */
  applied: number
  /** 被跳过的成交数：非法 / 重复 id / <= 尾段覆盖游标 */
  skipped: number
  /** 引擎已计入的最大成交 id；无则 null */
  throughTradeId: number | null
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
    byTime.set(t, { t, o, h, l, c, v: Number.isFinite(v) ? v : 0 })
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
  /** 已计入的实时成交 id（跨帧去重），随 coverage 提升裁剪 */
  private readonly seenTradeIds = new Set<number>()
  /** SSE 尾段已覆盖的成交 id：<= 此值的成交已在历史里，必须跳过 */
  private _coverageTradeId: number | null = null
  /** 已计入的最大实时成交 id */
  private _throughTradeId: number | null = null

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

  /** SSE 尾段覆盖到的最后成交 id；null 表示未知（退化为纯 id 去重）。 */
  get coverageTradeId(): number | null {
    return this._coverageTradeId
  }

  /** 已计入的最大实时成交 id。 */
  get throughTradeId(): number | null {
    return this._throughTradeId
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
   * `coverageTradeId` 来自 `history_tail_through_trade_id`：<= 该 id 的实时帧
   * 已经在历史里，`applyTrades` 会跳过，避免成交量重复累计。
   */
  load(points: readonly FxChartPoint[], coverageTradeId: number | null = null): void {
    this._candles = fxChartPointsToCandles(points)
    this.seenTradeIds.clear()
    this._throughTradeId = null
    this.setCoverageTradeId(coverageTradeId)
    this.rebuildMa(0)
  }

  /** 记录尾段覆盖游标；同时裁剪 <= 该 id 的去重集合，避免无界增长。 */
  setCoverageTradeId(id: number | null): void {
    this._coverageTradeId = Number.isSafeInteger(id) && (id as number) >= 0 ? id : null
    if (this._coverageTradeId === null) return
    for (const seen of this.seenTradeIds) {
      if (seen <= this._coverageTradeId) this.seenTradeIds.delete(seen)
    }
  }

  clear(): void {
    this._candles = []
    this.maCache.clear()
    this.seenTradeIds.clear()
    this._coverageTradeId = null
    this._throughTradeId = null
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
   * 用一条实时价格更新 forming candle：
   * - 无历史 → 新建第一根；
   * - 同一/落后 bucket → 原地更新最后一根（FX 帧是当前价，容忍客户端时钟略慢）；
   * - 新 bucket → 本地合成中间空桶；跨桶 > `FX_MAX_LOCAL_FILL_BUCKETS` 时要求 reload。
   * 价格非法（NaN/<=0）直接忽略。
   */
  applyPrice(price: number, tsMs: number = Date.now(), volume = 0): FxPriceApply {
    if (!Number.isFinite(price) || price <= 0) {
      return { reload: false, changed: [], added: 0 }
    }
    const safeVolume = Number.isFinite(volume) && volume > 0 ? volume : 0
    const bucket = fxBucketStart(Math.floor(tsMs / 1000), this.stepSeconds)
    const candles = this._candles

    if (candles.length === 0) {
      const candle: FxCandle = { t: bucket, o: price, h: price, l: price, c: price, v: safeVolume }
      candles.push(candle)
      this.rebuildMa(0)
      return { reload: false, changed: [candle], added: 1 }
    }

    const last = candles[candles.length - 1]!
    if (bucket > last.t) {
      const gapBuckets = (bucket - last.t) / this.stepSeconds - 1
      if (gapBuckets > FX_MAX_LOCAL_FILL_BUCKETS) {
        return { reload: true, changed: [], added: 0 }
      }
      const changed: FxCandle[] = []
      const prevClose = last.c
      for (let t = last.t + this.stepSeconds; t < bucket; t += this.stepSeconds) {
        const fill: FxCandle = { t, o: prevClose, h: prevClose, l: prevClose, c: prevClose, v: 0 }
        candles.push(fill)
        changed.push(fill)
      }
      const next: FxCandle = {
        t: bucket,
        o: prevClose,
        h: Math.max(prevClose, price),
        l: Math.min(prevClose, price),
        c: price,
        v: safeVolume,
      }
      candles.push(next)
      changed.push(next)
      this.rebuildMa(candles.length - changed.length)
      return { reload: false, changed, added: changed.length }
    }

    // bucket === last.t 或 bucket < last.t（客户端时钟略慢）→ 原地更新最后一根
    last.h = Math.max(last.h, price)
    last.l = Math.min(last.l, price)
    last.c = price
    last.v += safeVolume
    this.rebuildMa(candles.length - 1)
    return { reload: false, changed: [last], added: 0 }
  }

  /**
   * 批量应用真实成交（含金侧成交量与真实成交时间）：
   * - 按 `ts`（真实成交时间）分桶，不用客户端接收时间；
   * - `id <= coverageTradeId` 或重复 id 的成交被跳过，保证成交量不重复累计；
   * - 整批先预检桶缺口，超限/落在已知区间之前则 `reload: true` 且**不改动**任何 candle，
   *   由组件重读历史，而不是伪造 v=0 的桶冒充完整成交量；
   * - MA 只在批末重建一次。
   */
  applyTrades(trades: readonly FxCandleTrade[]): FxTradesApply {
    const result: FxTradesApply = {
      reload: false, changed: [], added: 0, applied: 0, skipped: 0,
      throughTradeId: this._throughTradeId,
    }
    if (trades.length === 0) return result

    const accepted: FxCandleTrade[] = []
    const batchIds = new Set<number>()
    for (const trade of trades) {
      if (!isValidTrade(trade)) {
        result.skipped += 1
        continue
      }
      if (this._coverageTradeId !== null && trade.id <= this._coverageTradeId) {
        result.skipped += 1
        continue
      }
      if (this.seenTradeIds.has(trade.id) || batchIds.has(trade.id)) {
        result.skipped += 1
        continue
      }
      batchIds.add(trade.id)
      accepted.push(trade)
    }
    if (accepted.length === 0) return result

    // 预检：整批要么全部可应用，要么要求 reload，避免半批写入后被迫重读。
    const first = this._candles[0]
    const last = this._candles[this._candles.length - 1]
    const lastT = last?.t
    let maxBucket = lastT
    for (const trade of accepted) {
      const bucket = fxBucketStart(Math.floor(trade.ts), this.stepSeconds)
      if (first && bucket < first.t) return { ...result, reload: true }
      if (lastT !== undefined && bucket <= lastT && findCandleIndex(this._candles, bucket) < 0) {
        return { ...result, reload: true }
      }
      if (maxBucket === undefined || bucket > maxBucket) maxBucket = bucket
    }
    if (lastT !== undefined && maxBucket !== undefined
      && maxBucket - lastT > (FX_MAX_LOCAL_FILL_BUCKETS + 1) * this.stepSeconds) {
      return { ...result, reload: true }
    }

    const sorted = [...accepted].sort((a, b) => a.ts - b.ts || a.id - b.id)
    const changedIndexes = new Set<number>()
    for (const trade of sorted) {
      const bucket = fxBucketStart(Math.floor(trade.ts), this.stepSeconds)
      const candles = this._candles
      if (candles.length === 0) {
        candles.push({ t: bucket, o: trade.price, h: trade.price, l: trade.price, c: trade.price, v: trade.volume })
        changedIndexes.add(0)
        result.added += 1
      } else {
        const currentLast = candles[candles.length - 1]!
        if (bucket > currentLast.t) {
          const prevClose = currentLast.c
          for (let t = currentLast.t + this.stepSeconds; t < bucket; t += this.stepSeconds) {
            candles.push({ t, o: prevClose, h: prevClose, l: prevClose, c: prevClose, v: 0 })
            changedIndexes.add(candles.length - 1)
            result.added += 1
          }
          candles.push({
            t: bucket,
            o: prevClose,
            h: Math.max(prevClose, trade.price),
            l: Math.min(prevClose, trade.price),
            c: trade.price,
            v: trade.volume,
          })
          changedIndexes.add(candles.length - 1)
          result.added += 1
        } else {
          const index = findCandleIndex(candles, bucket)
          if (index < 0) {
            // 已知区间内的空桶缺口（历史未 fill）：不伪造，明确要求 reload。
            return { ...result, reload: true, changed: [], added: 0 }
          }
          const candle = candles[index]!
          candle.h = Math.max(candle.h, trade.price)
          candle.l = Math.min(candle.l, trade.price)
          candle.c = trade.price
          candle.v += trade.volume
          changedIndexes.add(index)
        }
      }
      this.seenTradeIds.add(trade.id)
      this._throughTradeId = this._throughTradeId === null
        ? trade.id : Math.max(this._throughTradeId, trade.id)
      result.applied += 1
    }

    if (changedIndexes.size > 0) {
      const indexes = [...changedIndexes].sort((a, b) => a - b)
      this.rebuildMa(indexes[0]!)
      result.changed = indexes.map((index) => this._candles[index]!)
    }
    result.throughTradeId = this._throughTradeId
    return result
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

/** 真实成交输入合法性：id 安全非负、ts 有限正、价格有限正、量有限非负。 */
function isValidTrade(trade: FxCandleTrade): boolean {
  return Number.isSafeInteger(trade.id) && trade.id >= 0
    && Number.isFinite(trade.ts) && trade.ts > 0
    && Number.isFinite(trade.price) && trade.price > 0
    && Number.isFinite(trade.volume) && trade.volume >= 0
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
