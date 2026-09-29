// FX K 线增量内核行为测试：bucket 归类、空桶填充、forming candle 实时合并、
// MA10/MA20 增量维护、跨桶过多 reload、时钟漂移容错。断言可执行行为，不碰 DOM。
import { describe, expect, it } from 'vitest'
import {
  FxCandleEngine,
  FX_MAX_LOCAL_FILL_BUCKETS,
  fxBucketStart,
  fxChartPointsToCandles,
  fxMovingAverage,
  type FxCandle,
} from '@/utils/fxCandle'
import type { FxChartPoint } from '@/types/fx'

const BASE = Date.UTC(2026, 0, 1, 0, 0, 0) / 1000

const iso = (seconds: number) => new Date(seconds * 1000).toISOString()

const point = (seconds: number, c: number, v = 1): FxChartPoint => ({
  t: iso(seconds),
  o: c,
  h: c + 0.5,
  l: c - 0.5,
  c,
  v,
})

const series = (count: number, step = 60, start = BASE): FxChartPoint[] =>
  Array.from({ length: count }, (_, i) => point(start + i * step, i + 1, i))

const engineWith = (count: number, step = 60): FxCandleEngine => {
  const engine = new FxCandleEngine(step, [10, 20])
  engine.load(series(count, step))
  return engine
}

describe('fxBucketStart', () => {
  it('按周期向下对齐', () => {
    expect(fxBucketStart(1000, 60)).toBe(960)
    expect(fxBucketStart(900, 900)).toBe(900)
    expect(fxBucketStart(901, 900)).toBe(900)
    expect(fxBucketStart(1799, 900)).toBe(900)
    expect(fxBucketStart(1800, 900)).toBe(1800)
  })

  it('非法 step 原样返回', () => {
    expect(fxBucketStart(123, 0)).toBe(123)
    expect(fxBucketStart(123, Number.NaN)).toBe(123)
  })
})

describe('fxChartPointsToCandles', () => {
  it('按时间排序、丢弃非法行、同 bucket 保留最后一条', () => {
    const candles = fxChartPointsToCandles([
      point(BASE + 60, 1),
      { t: 'not-a-date', o: 1, h: 1, l: 1, c: 1, v: 1 },
      point(BASE, 2),
      point(BASE, 3),
      { t: iso(BASE + 120), o: Number.NaN, h: 1, l: 1, c: 1, v: 1 },
    ])
    expect(candles.map((c) => c.t)).toEqual([BASE, BASE + 60])
    expect(candles[0]!.c).toBe(3)
    expect(candles[1]!.c).toBe(1)
  })
})

describe('fxMovingAverage', () => {
  it('前 period-1 位为 NaN，之后为窗口均值', () => {
    const candles: FxCandle[] = [1, 2, 3, 4, 5].map((c, i) => ({
      t: BASE + i * 60,
      o: c,
      h: c,
      l: c,
      c,
      v: 0,
    }))
    const ma3 = fxMovingAverage(candles, 3)
    expect(Number.isNaN(ma3[0]!)).toBe(true)
    expect(Number.isNaN(ma3[1]!)).toBe(true)
    expect(ma3[2]).toBeCloseTo(2)
    expect(ma3[3]).toBeCloseTo(3)
    expect(ma3[4]).toBeCloseTo(4)
  })
})

describe('FxCandleEngine', () => {
  it('load 后 count 正确，MA10/MA20 增量算对', () => {
    const engine = engineWith(25)
    expect(engine.count).toBe(25)
    const ma10 = engine.ma(10)
    expect(Number.isNaN(ma10[8]!)).toBe(true)
    // closes 1..10 → 5.5
    expect(ma10[9]).toBeCloseTo(5.5)
    // closes 3..12 → 7.5
    expect(ma10[11]).toBeCloseTo(7.5)
    const ma20 = engine.ma(20)
    expect(Number.isNaN(ma20[18]!)).toBe(true)
    // closes 1..20 → 10.5
    expect(ma20[19]).toBeCloseTo(10.5)
  })

  it('同一 bucket 的价格 tick 原地更新 h/l/c/v 并刷新 MA', () => {
    const engine = engineWith(12)
    const lastBucket = BASE + 11 * 60
    const result = engine.applyPrice(100, lastBucket * 1000, 5)
    expect(result.reload).toBe(false)
    expect(result.added).toBe(0)
    expect(result.changed).toHaveLength(1)
    const last = engine.candles[11]!
    expect(last.c).toBe(100)
    expect(last.h).toBe(100)
    expect(last.l).toBe(11.5)
    expect(last.v).toBe(11 + 5)
    // 最后 10 根 closes = 3..11 + 100 = 163 → 16.3
    expect(engine.maAt(10, 11)).toBeCloseTo(16.3)
    // 再跌回 1.0：l 保持不变，h 保持 100
    engine.applyPrice(1, lastBucket * 1000)
    expect(engine.candles[11]!.l).toBe(1)
    expect(engine.candles[11]!.h).toBe(100)
  })

  it('跨 bucket 时本地合成空桶并新建 forming candle', () => {
    const engine = new FxCandleEngine(60, [10, 20])
    engine.load([point(BASE, 10)])
    const result = engine.applyPrice(20, (BASE + 3 * 60) * 1000)
    expect(result.reload).toBe(false)
    expect(result.added).toBe(3)
    expect(engine.count).toBe(4)
    expect(result.changed.map((c) => c.t)).toEqual([BASE + 60, BASE + 120, BASE + 180])
    expect(result.changed[0]).toMatchObject({ o: 10, h: 10, l: 10, c: 10, v: 0 })
    expect(result.changed[1]).toMatchObject({ o: 10, h: 10, l: 10, c: 10, v: 0 })
    expect(result.changed[2]).toMatchObject({ o: 10, h: 20, l: 10, c: 20 })
  })

  it('跨桶数超过上限时要求整段 reload 且不改动本地数据', () => {
    const engine = new FxCandleEngine(60, [10, 20])
    engine.load([point(BASE, 10)])
    const farSeconds = BASE + (FX_MAX_LOCAL_FILL_BUCKETS + 2) * 60
    const result = engine.applyPrice(5, farSeconds * 1000)
    expect(result.reload).toBe(true)
    expect(result.changed).toHaveLength(0)
    expect(engine.count).toBe(1)
  })

  it('落后 bucket 的 tick 更新最后一根（容忍客户端时钟略慢）', () => {
    const engine = new FxCandleEngine(60, [10, 20])
    engine.load([point(BASE, 10), point(BASE + 60, 12)])
    const result = engine.applyPrice(7, (BASE - 120) * 1000)
    expect(result.reload).toBe(false)
    expect(result.changed).toHaveLength(1)
    expect(engine.count).toBe(2)
    expect(engine.candles[1]!.c).toBe(7)
    expect(engine.candles[0]!.c).toBe(10)
  })

  it('无历史时第一根 tick 直接建 candle；非法价格被忽略', () => {
    const engine = new FxCandleEngine(60, [10, 20])
    const created = engine.applyPrice(0.2, BASE * 1000)
    expect(created.added).toBe(1)
    expect(engine.candles[0]!.c).toBe(0.2)
    expect(engine.applyPrice(Number.NaN, BASE * 1000).changed).toHaveLength(0)
    expect(engine.applyPrice(-1, BASE * 1000).changed).toHaveLength(0)
    expect(engine.count).toBe(1)
  })

  it('setStep 切换周期会清空，等待调用方重新 load', () => {
    const engine = engineWith(5)
    engine.setStep(900)
    expect(engine.step).toBe(900)
    expect(engine.count).toBe(0)
  })
})
