// FX K 线增量内核行为测试：bucket 归类、空桶填充、forming candle 实时合并、
// MA10/MA20 增量维护、跨桶过多 reload。断言可执行行为，不碰 DOM。
import { describe, expect, it } from 'vitest'
import {
  FxCandleEngine,
  FX_MAX_LOCAL_FILL_BUCKETS,
  fxBucketStart,
  fxChartPointsToCandles,
  fxMovingAverage,
  fxCandleVisibleRange,
  fxCandleScrolledBack,
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
  n_events: v === 0 ? 0 : 1,
  n_trades: v === 0 ? 0 : 1,
})

const series = (count: number, step = 60, start = BASE): FxChartPoint[] =>
  Array.from({ length: count }, (_, i) => point(start + i * step, i + 1, i))

const engineWith = (count: number, step = 60): FxCandleEngine => {
  const engine = new FxCandleEngine(step, [10, 20])
  engine.load(series(count, step))
  return engine
}

describe('fxCandleVisibleRange', () => {
  it('空历史没有可设置的时间范围，第一根成交后恢复跟随', () => {
    expect(fxCandleVisibleRange([], BASE, 60)).toBeNull()
    const candles = fxChartPointsToCandles([point(BASE + 60, 5)])
    expect(fxCandleVisibleRange(candles, BASE, 60)).toEqual({
      from: BASE + 120 - 80 * 60,
      to: BASE + 120,
    })
  })
  it('首笔成交自动显示最新桶不算回看，只有移到旧桶才暂停跟随', () => {
    const candles = fxChartPointsToCandles([point(BASE, 5), point(BASE + 60, 6)])
    // Chart reports the last visible real bucket, not the wall-clock endpoint.
    expect(fxCandleScrolledBack(candles, BASE + 60)).toBe(false)
    expect(fxCandleScrolledBack(candles, BASE)).toBe(true)
  })
})

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

  it('setStep 切换周期会清空，等待调用方重新 load', () => {
    const engine = engineWith(5)
    engine.setStep(900)
    expect(engine.step).toBe(900)
    expect(engine.count).toBe(0)
  })
})

describe('FxCandleEngine.applyEvents（真实成交增量）', () => {
  it('按真实成交时间分桶，重复 id / 尾段覆盖 id 不重复累计金侧量', () => {
    const engine = new FxCandleEngine(60, [10, 20])
    engine.load([point(BASE, 10, 1)], 100)   // 历史含 v=1；尾段覆盖到 id=100

    const first = engine.applyEvents([
      { id: 100, ts: BASE + 60, prePrice: 10, price: 11, volume: 5 },   // <= coverage → 跳过
      { id: 101, ts: BASE + 60, prePrice: 10, price: 11, volume: 5 },
      { id: 101, ts: BASE + 60, prePrice: 10, price: 11, volume: 5 },   // 同批重复 → 跳过
    ])
    expect(first.reload).toBe(false)
    expect(first.applied).toBe(1)
    expect(first.skipped).toBe(2)
    expect(engine.count).toBe(2)
    // 首笔交易将池价从10推到11，新桶应显示完整涨幅
    expect(engine.candles[1]).toMatchObject({ t: BASE + 60, o: 10, h: 11, l: 10, c: 11, v: 5 })

    // 断线重放同一笔成交：不得再次累计成交量
    const replay = engine.applyEvents([{ id: 101, ts: BASE + 60, prePrice: 10, price: 11, volume: 5 }])
    expect(replay.applied).toBe(0)
    expect(replay.skipped).toBe(1)
    expect(engine.candles[1]!.v).toBe(5)
    expect(engine.throughEventId).toBe(101)
  })

  it('合成空桶首笔成交以真实交易前价重置OHLC，不保留未知池变化之前的极值', () => {
    const engine = new FxCandleEngine(60)
    // merge 平推出的 forming 桶：o=h=l=c=10、v=0（合成空桶，非真实成交）
    engine.load([point(BASE, 10, 0)])
    const result = engine.applyEvents([
      { id: 1, ts: BASE + 5, prePrice: 9, price: 11, volume: 2 },   // 首笔定义整根 OHLC
      { id: 2, ts: BASE + 10, prePrice: 12, price: 12, volume: 3 },
      { id: 3, ts: BASE + 20, prePrice: 13, price: 13, volume: 4 },
    ])
    expect(result.reload).toBe(false)
    expect(result.applied).toBe(3)
    const candle = engine.candles[0]!
    expect(candle.o).toBe(9)   // 真实交易前价，不用旧空桶10或交易后11
    expect(candle.h).toBe(13)
    expect(candle.l).toBe(9)
    expect(candle.c).toBe(13)
    expect(candle.v).toBe(9)
  })

  it('桶缺口过大要求 reload，且不改动已有 candle/成交量', () => {
    const engine = new FxCandleEngine(60)
    engine.load([point(BASE, 10, 7)])
    const far = BASE + (FX_MAX_LOCAL_FILL_BUCKETS + 3) * 60
    const result = engine.applyEvents([{ id: 1, ts: far, prePrice: 20, price: 20, volume: 5 }])
    expect(result.reload).toBe(true)
    expect(result.applied).toBe(0)
    expect(engine.count).toBe(1)
    expect(engine.candles[0]!.v).toBe(7)
  })

  it('早于已知历史的成交要求 reload，避免静默丢弃或错桶累计', () => {
    const engine = new FxCandleEngine(60)
    engine.load([point(BASE, 10, 1), point(BASE + 60, 11, 1)])
    const result = engine.applyEvents([{ id: 5, ts: BASE - 120, prePrice: 9, price: 9, volume: 3 }])
    expect(result.reload).toBe(true)
    expect(engine.candles[0]!.v).toBe(1)
  })

  it('非法成交（负量 / 非安全 id / 非有限价）被跳过', () => {
    const engine = new FxCandleEngine(60)
    engine.load([point(BASE, 10, 1)])
    const result = engine.applyEvents([
      { id: -1, ts: BASE, prePrice: 10, price: 10, volume: 5 },
      { id: 1.5, ts: BASE, prePrice: 10, price: 10, volume: 5 },
      { id: 2, ts: BASE, prePrice: 10, price: 10, volume: -5 },
      { id: 3, ts: Number.NaN, prePrice: 10, price: 10, volume: 5 },
    ])
    expect(result.applied).toBe(0)
    expect(engine.candles[0]!.v).toBe(1)
  })

  it('实时创建的桶跨批次保留 (ts,id) 首末键：乱序回填 open、追加更新 close', () => {
    const engine = new FxCandleEngine(60)
    engine.load([])
    // 批次 1：以 id=1/ts=BASE+65 创建桶 BASE+60
    engine.applyEvents([{ id: 1, ts: BASE + 65, prePrice: 10, price: 10, volume: 1 }])
    expect(engine.candles[0]).toMatchObject({ t: BASE + 60, o: 10, c: 10 })

    // 批次 2：更高 id 但更早 ts（发布乱序）→ 成为新的 open，close 不动
    const result = engine.applyEvents([{ id: 2, ts: BASE + 61, prePrice: 13, price: 12, volume: 2 }])
    expect(result.reload).toBe(false)
    const candle = engine.candles[0]!
    expect(candle.o).toBe(13)
    expect(candle.c).toBe(10)
    expect(candle.h).toBe(13)
    expect(candle.l).toBe(10)
    expect(candle.v).toBe(3)

    // 批次 3：更晚 ts → 更新 close
    engine.applyEvents([{ id: 3, ts: BASE + 70, prePrice: 8, price: 8, volume: 4 }])
    expect(engine.candles[0]).toMatchObject({ o: 13, c: 8, h: 13, l: 8, v: 7 })
  })

  it('预加载的更早桶出现乱序成交时要求 reload，不猜 O/C', () => {
    const engine = new FxCandleEngine(60)
    engine.load([point(BASE, 10, 1), point(BASE + 60, 11, 1), point(BASE + 120, 12, 1)])
    // 成交落在中间预加载桶（非 forming 末桶）→ 无历史首末键，无法判定时序
    const result = engine.applyEvents([{ id: 5, ts: BASE + 61, prePrice: 99, price: 99, volume: 3 }])
    expect(result.reload).toBe(true)
    expect(engine.candles[1]!.v).toBe(1)
  })
})


describe('event-only zero-volume continuity', () => {
  it('trade → funding → trade preserves every price and only real volume across reload/replay', () => {
    const engine = new FxCandleEngine(60)
    const events = [
      { id: 1, ts: BASE + 1, prePrice: 10, price: 11, volume: 2, nTrades: 1 },
      { id: 2, ts: BASE + 2, prePrice: 11, price: 20, volume: 0, nTrades: 0 },
      { id: 3, ts: BASE + 3, prePrice: 20, price: 12, volume: 3, nTrades: 1 },
    ]
    engine.applyEvents(events)
    expect(engine.candles[0]).toMatchObject({ o: 10, h: 20, l: 10, c: 12, v: 5, n_events: 3, n_trades: 2 })
    engine.load([{ t: iso(BASE), o: 10, h: 20, l: 10, c: 12, v: 5, n_events: 3, n_trades: 2 }], 3)
    expect(engine.applyEvents(events).applied).toBe(0)
    engine.applyEvents([{ id: 4, ts: BASE + 4, prePrice: 12, price: 13, volume: 1, nTrades: 1 }])
    expect(engine.candles[0]).toMatchObject({ o: 10, h: 20, l: 10, c: 13, v: 6, n_events: 4, n_trades: 3 })
  })

  it('loaded funding OHLC survives a later trade and quiet carried-close buckets', () => {
    const engine = new FxCandleEngine(60)
    engine.load([{ t: iso(BASE), o: 10, h: 20, l: 10, c: 20, v: 0, n_events: 1, n_trades: 0 }], 7)
    engine.applyEvents([{ id: 8, ts: BASE + 5, prePrice: 20, price: 15, volume: 3, nTrades: 1 }])
    expect(engine.candles[0]).toMatchObject({ o: 10, h: 20, l: 10, c: 15, v: 3, n_events: 2, n_trades: 1 })
    engine.carryTo(BASE + 180)
    expect(engine.candles.slice(1).map(c => [c.o, c.h, c.l, c.c, c.v, c.n_events])).toEqual([
      [15, 15, 15, 15, 0, 0], [15, 15, 15, 15, 0, 0], [15, 15, 15, 15, 0, 0],
    ])
    engine.applyEvents([{ id: 9, ts: BASE + 125, prePrice: 15, price: 18, volume: 0, nTrades: 0 }])
    expect(engine.candles.at(-1)).toMatchObject({ o: 18, h: 18, l: 18, c: 18, v: 0, n_events: 0 })
    engine.applyEvents([{ id: 10, ts: BASE + 185, prePrice: 18, price: 19, volume: 2, nTrades: 1 }])
    expect(engine.candles.at(-1)).toMatchObject({ o: 18, h: 19, l: 18, c: 19, v: 2, n_events: 1 })
  })

  it('unknown event count requests history instead of overwriting zero-volume prices', () => {
    const engine = new FxCandleEngine(60)
    engine.load([{ t: iso(BASE), o: 10, h: 20, l: 10, c: 20, v: 0 }])
    const before = engine.candles[0]!.c
    expect(engine.applyEvents([{ id: 2, ts: BASE + 5, prePrice: 20, price: 15, volume: 3 }]).reload).toBe(true)
    expect(engine.candles[0]!.c).toBe(before)
    expect(engine.carryTo(BASE + 60).added).toBe(0)
  })
})
