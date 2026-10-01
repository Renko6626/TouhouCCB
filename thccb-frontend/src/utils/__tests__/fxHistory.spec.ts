// FX 历史适配层纯工具 + 加载器行为测试。
// 覆盖真实故障：封存段/尾段重叠导致成交量重复、缺段被伪装成完整历史、
// 结构非法的段/逐笔成交混入公开数据（可见性防线）、段/窗口超限被静默截断、
// 以及同版本尾段替换触发重复封存段请求。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref } from 'vue'

vi.mock('@/api/fx', () => ({
  fxApi: { getChart: vi.fn(), getChartWithMeta: vi.fn() },
  fetchFxHistorySegment: vi.fn(),
  getChartWithMeta: vi.fn(),
}))

import { fetchFxHistorySegment, getChartWithMeta } from '@/api/fx'
import {
  FX_HISTORY_INTERVAL_SECONDS,
  FX_HISTORY_MAX_SEGMENTS,
  FX_HISTORY_SEGMENT_SECONDS,
  decodeFxHistorySegment,
  fxHistorySegmentEpochs,
  mergeFxHistoryCandles,
  sanitizeFxHistorySegment,
  sanitizeFxHistoryTail,
  sanitizeFxTradeTicks,
} from '@/utils/fxHistory'
import { loadFxHistoryCandles, loadFxHistoryResult, useFxCandleHistory } from '@/composables/useFxCandleHistory'
import type { FxChartPoint, FxHistorySegment, FxHistorySnapshotTail } from '@/types/fx'

const NOW_MS = Date.UTC(2026, 5, 1, 12, 30, 0)   // 2026-06-01T12:30:00Z
const NOW_SEC = Math.floor(NOW_MS / 1000)
const BOUNDARY = NOW_SEC - 1800                  // 12:00，1m 段边界

const iso = (seconds: number) => new Date(seconds * 1000).toISOString()

const point = (seconds: number, c: number, v = 0): FxChartPoint => ({
  t: iso(seconds), o: c, h: c, l: c, c, v,
})

const segment = (t0: number, rows: Array<[number, number, number]>, nBuckets = 60): FxHistorySegment => ({
  t0,
  step: 60,
  n_buckets: nBuckets,
  t: rows.map(([offset]) => offset),
  o: rows.map(([, price]) => price.toFixed(8)),
  h: rows.map(([, price]) => price.toFixed(8)),
  l: rows.map(([, price]) => price.toFixed(8)),
  c: rows.map(([, price]) => price.toFixed(8)),
  v: rows.map(([, , volume]) => volume.toFixed(6)),
  trades: rows.map(() => 1),
})

const chartMeta = (points: FxChartPoint[], throughTradeId = 99, historyVersion = 'v1') =>
  ({ points, throughTradeId, historyVersion })

const findAt = (points: FxChartPoint[], seconds: number): FxChartPoint | undefined =>
  points.find((p) => p.t === iso(seconds))

/** 冲掉被 mock 的 promise 链（fake timers 下不使用 setTimeout）。 */
const flush = async () => {
  for (let i = 0; i < 50; i++) await Promise.resolve()
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW_MS)
  vi.mocked(getChartWithMeta).mockReset()
  vi.mocked(fetchFxHistorySegment).mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('FX history 常量与段定位', () => {
  it('封存段长与后端 RING_SPEC 一致', () => {
    expect(FX_HISTORY_SEGMENT_SECONDS).toEqual({ '10s': 600, '1m': 3600, '15m': 86400, '1h': 604800 })
    expect(FX_HISTORY_INTERVAL_SECONDS).toEqual({ '10s': 10, '1m': 60, '15m': 900, '1h': 3600 })
  })

  it('fxHistorySegmentEpochs 只含已封存段，覆盖 lookback 起点', () => {
    // from=10:25 → 对齐 10:00；boundary=12:00，故封存段为 [10:00, 11:00]
    const epochs = fxHistorySegmentEpochs('1m', NOW_SEC - 7500, NOW_SEC)
    expect(epochs).toEqual([BOUNDARY - 7200, BOUNDARY - 3600])
    expect(epochs.every((e) => e < BOUNDARY)).toBe(true)
  })

  it('段数超过上限时显式抛错，不截断旧段', () => {
    const from = NOW_SEC - (FX_HISTORY_MAX_SEGMENTS + 1) * FX_HISTORY_SEGMENT_SECONDS['1h']
    expect(() => fxHistorySegmentEpochs('1h', from, NOW_SEC)).toThrow(RangeError)
  })
})

describe('sanitizeFxHistorySegment', () => {
  it('接受合法十进制字符串段并保留字符串价格/量', () => {
    const parsed = sanitizeFxHistorySegment(segment(1000, [[0, 1.25, 3]]))
    expect(parsed?.o).toEqual(['1.25000000'])
    expect(parsed?.v).toEqual(['3.000000'])
  })

  it('列长不一致 / 负量 / 非字符串价格 / 越界偏移都整段拒绝', () => {
    const good = segment(1000, [[0, 1, 1]])
    expect(sanitizeFxHistorySegment({ ...good, t: [0, 1] })).toBeNull()
    expect(sanitizeFxHistorySegment({ ...good, v: ['-1.000000'] })).toBeNull()
    expect(sanitizeFxHistorySegment({ ...good, o: [1] })).toBeNull()
    expect(sanitizeFxHistorySegment({ ...good, t: [60] })).toBeNull()
  })

  it('上下文校验 step/t0 对齐与 high >= low', () => {
    const good = segment(1200, [[0, 1, 1]], 60)
    expect(sanitizeFxHistorySegment(good, { interval: '1m', t0: 1200 })).not.toBeNull()
    expect(sanitizeFxHistorySegment({ ...good, step: 900 }, { interval: '1m' })).toBeNull()
    expect(sanitizeFxHistorySegment(good, { interval: '1m', t0: 2400 })).toBeNull()
    expect(sanitizeFxHistorySegment({ ...good, n_buckets: 30 }, { interval: '1m', t0: 1200 })).toBeNull()
    expect(sanitizeFxHistorySegment({ ...good, h: ['0.5'], l: ['1.0'] })).toBeNull()
  })

  it('history_tail 丢弃非法 interval，保留合法段', () => {
    const tail = sanitizeFxHistoryTail({
      '1m': segment(1200, [[0, 1, 1]]),
      '10s': { t0: 1, step: 10, n_buckets: 60, t: [0], o: [1], h: [1], l: [1], c: [1], v: [1], trades: [1] },
      bogus: segment(1200, [[0, 1, 1]]),
    })
    expect(Object.keys(tail ?? {})).toEqual(['1m'])
  })
})

describe('decodeFxHistorySegment', () => {
  it('稀疏桶按 t0 + offset*step 定位，价格/量只在解码边界转 number', () => {
    const points = decodeFxHistorySegment(segment(1_755_734_400, [[0, 1.5, 2], [3, 2.5, 4]]))
    expect(points).toHaveLength(2)
    expect(points[0]).toEqual({ t: iso(1_755_734_400), o: 1.5, h: 1.5, l: 1.5, c: 1.5, v: 2 })
    expect(points[1]!.t).toBe(iso(1_755_734_400 + 180))
    expect(points[1]!.c).toBe(2.5)
  })
})

describe('sanitizeFxTradeTicks', () => {
  it('只保留白名单字段，非法项被丢弃', () => {
    const ticks = sanitizeFxTradeTicks([
      { id: 1, ts: iso(NOW_SEC), post_price: '1.25000000', gold_volume: '3.000000', account_id: 7 },
      { id: 2, ts: 'nope', post_price: '1', gold_volume: '1' },
      { id: -1, ts: iso(NOW_SEC), post_price: '1', gold_volume: '1' },
      { id: 3, ts: iso(NOW_SEC), post_price: '1', gold_volume: '-1' },
    ])
    expect(ticks).toHaveLength(1)
    expect(ticks[0]).toEqual({ id: 1, ts: iso(NOW_SEC), post_price: '1.25000000', gold_volume: '3.000000' })
  })
})

describe('mergeFxHistoryCandles', () => {
  it('同桶封存段优先，不重复累计尾段成交量；缺桶用 prev_close 平推 v=0', () => {
    const epoch = 1_755_734_400
    const merged = mergeFxHistoryCandles(
      [point(epoch, 1, 1)],            // 封存段：同桶权威
      [point(epoch, 2, 9)],            // 尾段重叠：必须被丢弃
      60, epoch, epoch + 180,
    )
    expect(merged).toHaveLength(3)
    expect(merged[0]).toMatchObject({ c: 1, v: 1 })
    expect(merged[1]).toMatchObject({ o: 1, c: 1, v: 0 })   // 空桶，不是尾段的 v=9
    expect(merged[2]).toMatchObject({ o: 1, c: 1, v: 0 })
  })

  it('完全无数据返回空，不伪造横线', () => {
    expect(mergeFxHistoryCandles([], [], 60, 1000, 1300)).toEqual([])
  })
})

describe('loadFxHistoryResult', () => {
  const freshTail: FxHistorySnapshotTail = {
    history_version: 'v1',
    history_tail: { '1m': segment(BOUNDARY, [[0, 2, 5], [10, 3, 6]], 31) },
    history_tail_at: iso(NOW_SEC),
    history_tail_through_trade_id: 42,
    history_ready: true,
  }

  it('就绪时读封存段 + 新鲜尾段，游标用尾段覆盖 id 且不请求 /chart', async () => {
    vi.mocked(fetchFxHistorySegment).mockResolvedValue(segment(BOUNDARY - 3600, [[0, 1, 1], [30, 2, 2]]))
    const result = await loadFxHistoryResult(7, '1m', 60, freshTail)

    expect(getChartWithMeta).not.toHaveBeenCalled()
    expect(fetchFxHistorySegment).toHaveBeenCalledTimes(1)
    expect(fetchFxHistorySegment).toHaveBeenCalledWith(7, 'v1', '1m', BOUNDARY - 3600)
    expect(result.throughTradeId).toBe(42)
    expect(result.historyVersion).toBe('v1')
    expect(findAt(result.points, BOUNDARY - 1800)).toMatchObject({ c: 2, v: 2 })  // 11:30 封存段
    expect(findAt(result.points, BOUNDARY)).toMatchObject({ c: 2, v: 5 })         // 12:00 尾段
    expect(findAt(result.points, BOUNDARY + 600)).toMatchObject({ c: 3, v: 6 })   // 12:10 尾段
    expect(findAt(result.points, NOW_SEC)).toMatchObject({ v: 0 })                // forming 空桶
    // 成交量只累计一次：2（封存） + 5 + 6（尾段）；11:00 的 v=1 在窗口外
    expect(result.points.reduce((sum, p) => sum + p.v, 0)).toBe(13)
  })

  it('封存段缺失时回退 /chart，游标用 HTTP 响应头而不是旧 SSE 游标', async () => {
    vi.mocked(fetchFxHistorySegment).mockResolvedValue(null)
    vi.mocked(getChartWithMeta).mockResolvedValue(chartMeta([point(BOUNDARY, 9, 9)], 1234, 'v2'))
    const result = await loadFxHistoryResult(7, '1m', 60, freshTail)

    expect(getChartWithMeta).toHaveBeenCalledTimes(1)
    expect(result.throughTradeId).toBe(1234)
    expect(result.historyVersion).toBe('v2')
    expect(findAt(result.points, BOUNDARY)).toMatchObject({ c: 9, v: 9 })
  })

  it('无 history_version 或 history_ready=false 时回退 /chart', async () => {
    vi.mocked(getChartWithMeta).mockResolvedValue(chartMeta([point(BOUNDARY, 4, 4)]))
    await loadFxHistoryResult(7, '1m', 60, { ...freshTail, history_version: null })
    await loadFxHistoryResult(7, '1m', 60, { ...freshTail, history_ready: false })

    expect(fetchFxHistorySegment).not.toHaveBeenCalled()
    expect(getChartWithMeta).toHaveBeenCalledTimes(2)
  })

  it('尾段过期时只补 [封存边界, now]，游标用该 HTTP 响应头', async () => {
    vi.mocked(fetchFxHistorySegment).mockResolvedValue(segment(BOUNDARY - 3600, [[30, 2, 2]]))
    vi.mocked(getChartWithMeta).mockResolvedValue(chartMeta([point(BOUNDARY, 7, 7)], 555, 'v1'))
    const stale: FxHistorySnapshotTail = { ...freshTail, history_tail_at: iso(NOW_SEC - 7200) }
    const result = await loadFxHistoryResult(7, '1m', 60, stale)

    expect(fetchFxHistorySegment).toHaveBeenCalledTimes(1)
    expect(getChartWithMeta).toHaveBeenCalledWith(7, '1m', iso(BOUNDARY), iso(NOW_SEC))
    expect(result.throughTradeId).toBe(555)
    expect(findAt(result.points, BOUNDARY)).toMatchObject({ c: 7, v: 7 })
    expect(findAt(result.points, BOUNDARY - 1800)).toMatchObject({ c: 2, v: 2 })
  })

  it('缺少响应头元数据时显式报错，不假装旧游标有效', async () => {
    vi.mocked(getChartWithMeta).mockRejectedValue(new Error('FX chart response is missing history metadata'))
    await expect(loadFxHistoryResult(7, '1m', 60, { ...freshTail, history_version: null }))
      .rejects.toThrow('missing history metadata')
  })

  it('旧 loadFxHistoryCandles 包装仍只返回点', async () => {
    vi.mocked(getChartWithMeta).mockResolvedValue(chartMeta([point(BOUNDARY, 3, 3)]))
    const points = await loadFxHistoryCandles(7, '1m', 60, { ...freshTail, history_version: null })
    expect(findAt(points, BOUNDARY)).toMatchObject({ c: 3, v: 3 })
  })
})

describe('useFxCandleHistory 重载时机', () => {
  const freshTail: FxHistorySnapshotTail = {
    history_version: 'v1',
    history_tail: { '1m': segment(BOUNDARY, [[0, 2, 5]], 31) },
    history_tail_at: iso(NOW_SEC),
    history_tail_through_trade_id: 42,
    history_ready: true,
  }

  it('同一 history_version 下替换 tail 对象不重复请求封存段', async () => {
    vi.mocked(fetchFxHistorySegment).mockResolvedValue(segment(BOUNDARY - 3600, [[30, 2, 2]]))
    const tailRef = ref<FxHistorySnapshotTail | null>({ ...freshTail })
    const scope = effectScope()
    let state: ReturnType<typeof useFxCandleHistory> | undefined
    scope.run(() => {
      state = useFxCandleHistory({
        pairId: () => 7,
        interval: () => '1m',
        lookbackMinutes: () => 60,
        tail: tailRef,
      })
    })
    await flush()
    expect(fetchFxHistorySegment).toHaveBeenCalledTimes(1)
    expect(state!.throughTradeId.value).toBe(42)

    tailRef.value = { ...freshTail }               // 同版本、新对象引用
    await nextTick()
    await flush()
    expect(fetchFxHistorySegment).toHaveBeenCalledTimes(1)

    tailRef.value = { ...freshTail, history_version: 'v2' }
    await nextTick()
    await flush()
    expect(fetchFxHistorySegment).toHaveBeenCalledTimes(2)
    scope.stop()
  })
})
