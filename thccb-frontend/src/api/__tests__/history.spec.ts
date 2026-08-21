import { describe, expect, it } from 'vitest'
import {
  decodeSegment, fillCandles, sealedSegmentEpochs,
  SEGMENT_SECONDS, type EncodedSegment,
} from '../history'

const seg: EncodedSegment = {
  t0: 1755734400, step: 60, n_buckets: 60,
  t: [0, 3],
  o: [50000000, 60000000], h: [70000000, 60000000],
  l: [50000000, 40000000], c: [60000000, 55000000],
  v: [1.5, 2], trades: [1, 2],
}

describe('decodeSegment', () => {
  it('定点 ÷1e8、稀疏桶按 t0+t[i]*step 定位', () => {
    const candles = decodeSegment(seg)
    expect(candles).toHaveLength(2)
    expect(candles[0]).toEqual({
      t: new Date(1755734400 * 1000).toISOString(),
      o: 0.5, h: 0.7, l: 0.5, c: 0.6, v: 1.5, n: 1,
    })
    expect(candles[1]!.t).toBe(new Date((1755734400 + 180) * 1000).toISOString())
    expect(candles[1]!.c).toBe(0.55)
  })
  it('空段解码为空数组', () => {
    expect(decodeSegment({ ...seg, t: [], o: [], h: [], l: [], c: [], v: [], trades: [] })).toEqual([])
  })
})

describe('sealedSegmentEpochs', () => {
  it('只含完全封存的段，覆盖 lookback 起点所在段', () => {
    expect(SEGMENT_SECONDS['1m']).toBe(3600)
    const now = 1755734400 + 3600 + 120        // 当前 1h 段进行中
    const from = 1755734400 - 1800             // 上上段中部
    const epochs = sealedSegmentEpochs('1m', from, now)
    expect(epochs).toEqual([1755734400 - 3600, 1755734400])   // 进行中段不含
  })
  it('lookback 全在当前段内时返回空', () => {
    const now = 1755734400 + 300
    expect(sealedSegmentEpochs('1m', 1755734400 + 60, now)).toEqual([])
  })
})

describe('fillCandles', () => {
  const c = (epoch: number, price: number, v = 1, n = 1) => ({
    t: new Date(epoch * 1000).toISOString(),
    o: price, h: price, l: price, c: price, v, n,
  })
  it('缺桶用 prev_close 平推，v=0 n=0（对齐后端 fill=true）', () => {
    const out = fillCandles([c(1000000020, 0.6)], 10, 1000000000, 1000000050)
    expect(out).toHaveLength(5)
    expect(out[0]).toMatchObject({ o: 0.6, c: 0.6, v: 0, n: 0 })   // 前置空桶用首根 open 回填
    expect(out[2]).toMatchObject({ o: 0.6, c: 0.6, v: 1 })
    expect(out[3]).toMatchObject({ o: 0.6, c: 0.6, v: 0, n: 0 })
  })
  it('完全无数据返回空（无 prev_close 可推）', () => {
    expect(fillCandles([], 10, 1000000000, 1000000050)).toEqual([])
  })
})
