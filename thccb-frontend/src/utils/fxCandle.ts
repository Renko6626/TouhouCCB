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

  /** 切换周期：步长变化时清空（调用方随后会重新 load）。 */
  setStep(stepSeconds: number): void {
    const next = FX_CANDLE_STEP(stepSeconds)
    if (next === this.stepSeconds) return
    this.stepSeconds = next
    this.clear()
  }

  /** 用整段历史重置（初始加载 / 切周期 / gap reload）。 */
  load(points: readonly FxChartPoint[]): void {
    this._candles = fxChartPointsToCandles(points)
    this.rebuildMa(0)
  }

  clear(): void {
    this._candles = []
    this.maCache.clear()
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
