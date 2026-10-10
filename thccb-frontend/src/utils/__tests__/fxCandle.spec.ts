// FX K 线增量内核行为测试：bucket 归类、空桶填充、forming candle 实时合并、
// MA10/MA20 增量维护、跨桶过多 reload、时钟漂移容错。断言可执行行为，不碰 DOM。
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

describe('FxCandleEngine.applyTrades（真实成交增量）', () => {
  it('按真实成交时间分桶，重复 id / 尾段覆盖 id 不重复累计金侧量', () => {
    const engine = new FxCandleEngine(60, [10, 20])
    engine.load([point(BASE, 10, 1)], 100)   // 历史含 v=1；尾段覆盖到 id=100

    const first = engine.applyTrades([
      { id: 100, ts: BASE + 60, price: 11, volume: 5 },   // <= coverage → 跳过
      { id: 101, ts: BASE + 60, price: 11, volume: 5 },
      { id: 101, ts: BASE + 60, price: 11, volume: 5 },   // 同批重复 → 跳过
    ])
    expect(first.reload).toBe(false)
    expect(first.applied).toBe(1)
    expect(first.skipped).toBe(2)
    expect(engine.count).toBe(2)
    // 有真实成交的新桶 O/H/L/C 从首笔 post_price=11 开始，不用 prevClose=10
    expect(engine.candles[1]).toMatchObject({ t: BASE + 60, o: 11, h: 11, l: 11, c: 11, v: 5 })

    // 断线重放同一笔成交：不得再次累计成交量
    const replay = engine.applyTrades([{ id: 101, ts: BASE + 60, price: 11, volume: 5 }])
    expect(replay.applied).toBe(0)
    expect(replay.skipped).toBe(1)
    expect(engine.candles[1]!.v).toBe(5)
    expect(engine.throughTradeId).toBe(101)
  })

  it('合成空桶（v=0）的首笔真实成交重置 O/H/L/C，不保留 carry prevClose/极值', () => {
    const engine = new FxCandleEngine(60)
    // merge 平推出的 forming 桶：o=h=l=c=10、v=0（合成空桶，非真实成交）
    engine.load([point(BASE, 10, 0)])
    const result = engine.applyTrades([
      { id: 1, ts: BASE + 5, price: 11, volume: 2 },   // 首笔定义整根 OHLC
      { id: 2, ts: BASE + 10, price: 12, volume: 3 },
      { id: 3, ts: BASE + 20, price: 13, volume: 4 },
    ])
    expect(result.reload).toBe(false)
    expect(result.applied).toBe(3)
    const candle = engine.candles[0]!
    expect(candle.o).toBe(11)   // 首笔 post_price，而不是 carry 的 10
    expect(candle.h).toBe(13)
    expect(candle.l).toBe(11)   // 不能把 carry 的 10 当成最低价
    expect(candle.c).toBe(13)
    expect(candle.v).toBe(9)
  })

  it('桶缺口过大要求 reload，且不改动已有 candle/成交量', () => {
    const engine = new FxCandleEngine(60)
    engine.load([point(BASE, 10, 7)])
    const far = BASE + (FX_MAX_LOCAL_FILL_BUCKETS + 3) * 60
    const result = engine.applyTrades([{ id: 1, ts: far, price: 20, volume: 5 }])
    expect(result.reload).toBe(true)
    expect(result.applied).toBe(0)
    expect(engine.count).toBe(1)
    expect(engine.candles[0]!.v).toBe(7)
  })

  it('早于已知历史的成交要求 reload，避免静默丢弃或错桶累计', () => {
    const engine = new FxCandleEngine(60)
    engine.load([point(BASE, 10, 1), point(BASE + 60, 11, 1)])
    const result = engine.applyTrades([{ id: 5, ts: BASE - 120, price: 9, volume: 3 }])
    expect(result.reload).toBe(true)
    expect(engine.candles[0]!.v).toBe(1)
  })

  it('非法成交（负量 / 非安全 id / 非有限价）被跳过', () => {
    const engine = new FxCandleEngine(60)
    engine.load([point(BASE, 10, 1)])
    const result = engine.applyTrades([
      { id: -1, ts: BASE, price: 10, volume: 5 },
      { id: 1.5, ts: BASE, price: 10, volume: 5 },
      { id: 2, ts: BASE, price: 10, volume: -5 },
      { id: 3, ts: Number.NaN, price: 10, volume: 5 },
    ])
    expect(result.applied).toBe(0)
    expect(engine.candles[0]!.v).toBe(1)
  })

  it('实时创建的桶跨批次保留 (ts,id) 首末键：乱序回填 open、追加更新 close', () => {
    const engine = new FxCandleEngine(60)
    engine.load([])
    // 批次 1：以 id=1/ts=BASE+65 创建桶 BASE+60
    engine.applyTrades([{ id: 1, ts: BASE + 65, price: 10, volume: 1 }])
    expect(engine.candles[0]).toMatchObject({ t: BASE + 60, o: 10, c: 10 })

    // 批次 2：更高 id 但更早 ts（发布乱序）→ 成为新的 open，close 不动
    const result = engine.applyTrades([{ id: 2, ts: BASE + 61, price: 12, volume: 2 }])
    expect(result.reload).toBe(false)
    const candle = engine.candles[0]!
    expect(candle.o).toBe(12)
    expect(candle.c).toBe(10)
    expect(candle.h).toBe(12)
    expect(candle.l).toBe(10)
    expect(candle.v).toBe(3)

    // 批次 3：更晚 ts → 更新 close
    engine.applyTrades([{ id: 3, ts: BASE + 70, price: 8, volume: 4 }])
    expect(engine.candles[0]).toMatchObject({ o: 12, c: 8, h: 12, l: 8, v: 7 })
  })

  it('预加载的更早桶出现乱序成交时要求 reload，不猜 O/C', () => {
    const engine = new FxCandleEngine(60)
    engine.load([point(BASE, 10, 1), point(BASE + 60, 11, 1), point(BASE + 120, 12, 1)])
    // 成交落在中间预加载桶（非 forming 末桶）→ 无历史首末键，无法判定时序
    const result = engine.applyTrades([{ id: 5, ts: BASE + 61, price: 99, volume: 3 }])
    expect(result.reload).toBe(true)
    expect(engine.candles[1]!.v).toBe(1)
  })
})
