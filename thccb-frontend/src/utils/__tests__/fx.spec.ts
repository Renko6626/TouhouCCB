// FX Task 8 行为测试：金额格式化、min-out/滑点、SSE 公开帧白名单、错误映射。
// 断言的是可执行行为，不是源码字符串或恒真 identity。
import { describe, expect, it } from 'vitest'
import {
  computeMinOut,
  formatFxAmount,
  FxOrderSubmitter,
  fxOrderSignature,
  isConflictError,
  mapFxError,
  mergeFxPairs,
  newFxIdempotencyKey,
  normalizeFxCandles,
  parseFxFrame,
  parseFxSsePayload,
  sanitizeFxFrame,
  tradeSlippageBps,
  upsertFxPairAdmin,
} from '@/api/fx'
import type { FxPairAdmin, FxPairPublic } from '@/types/fx'

describe('formatFxAmount（十进制字符串格式化，不丢精度）', () => {
  it('固定 6 位并正确 half-up 舍入', () => {
    expect(formatFxAmount('1')).toBe('1.000000')
    expect(formatFxAmount('0.123456789')).toBe('0.123457')
    expect(formatFxAmount('10.9999995', 6)).toBe('11.000000')
    expect(formatFxAmount('123456789012345678.5')).toBe('123456789012345678.500000')
  })

  it('支持自定义小数位与整数舍入', () => {
    expect(formatFxAmount('1.005', 2)).toBe('1.01')
    expect(formatFxAmount('1.004', 2)).toBe('1.00')
    expect(formatFxAmount('9.999', 0)).toBe('10')
    expect(formatFxAmount('-1.5', 0)).toBe('-2')
  })

  it('非法/空输入返回占位符', () => {
    expect(formatFxAmount(null)).toBe('—')
    expect(formatFxAmount(undefined)).toBe('—')
    expect(formatFxAmount('')).toBe('—')
    expect(formatFxAmount('abc')).toBe('—')
  })
})

describe('min-out 与滑点', () => {
  it('按 bps 折扣并向下取 6 位，保证不放宽服务端滑点保护', () => {
    expect(computeMinOut('100', 100)).toBe('99.000000')
    // 100.123456 × (1 - 50/10000) = 99.62283872 → 向下取 99.622838
    expect(computeMinOut('100.123456', 50)).toBe('99.622838')
    expect(computeMinOut('1', 0)).toBe('1.000000')
    expect(computeMinOut('1', 10000)).toBe('0.000000')
    expect(computeMinOut('bad', 50)).toBe('0.000000')
  })

  it('滑点 = |有效价 − 中间价| / 中间价（bps）', () => {
    expect(tradeSlippageBps('1', '1.01')).toBeCloseTo(100, 9)
    expect(tradeSlippageBps('2', '1.98')).toBeCloseTo(100, 9)
    expect(tradeSlippageBps('1', '1')).toBe(0)
    expect(tradeSlippageBps('0', '1')).toBeNull()
    expect(tradeSlippageBps(null, '1')).toBeNull()
  })
})

describe('SSE 公开帧白名单', () => {
  it('只保留行情/公开新闻字段，丢弃 target/shock/future orders/random state', () => {
    const out = sanitizeFxFrame({
      price: '1.5',
      buy_price: '1.51',
      sell_price: '1.49',
      spread: '0.02',
      volume: '100',
      target_price: '9.9',
      shock_ratio: '0.2',
      future_orders: [1, 2],
      random_state: { seed: 1 },
      parameter_snapshot: { spent: '5' },
      news: {
        title: 'n',
        body: 'b',
        kind: 'macro',
        published_at: '2026-01-01T00:00:00Z',
        shock_ratio: '0.2',
      },
    })
    expect(out).not.toBeNull()
    expect(Object.keys(out!)).toEqual([
      'price',
      'buy_price',
      'sell_price',
      'spread',
      'volume',
      'news',
    ])
    expect(out!.news).toEqual({
      title: 'n',
      body: 'b',
      kind: 'macro',
      published_at: '2026-01-01T00:00:00Z',
    })
    const serialized = JSON.stringify(out)
    expect(serialized).not.toContain('target_price')
    expect(serialized).not.toContain('shock_ratio')
    expect(serialized).not.toContain('future_orders')
    expect(serialized).not.toContain('random_state')
    expect(serialized).not.toContain('parameter_snapshot')
  })

  it('裸帧与 SSE 信封都能解析，非法输入返回 null', () => {
    expect(parseFxFrame('{"price":"1.5","target_price":"9"}')).toEqual({ price: '1.5' })
    expect(parseFxFrame('not json')).toBeNull()
    expect(parseFxFrame(null)).toBeNull()
    expect(
      parseFxSsePayload(
        JSON.stringify({
          type: 'fx',
          market_id: 1,
          ts: 't',
          seq: 3,
          data: { price: '2', target_after: '3' },
        }),
      ),
    ).toEqual({ price: '2' })
    expect(sanitizeFxFrame('nope')).toBeNull()
  })
})

describe('normalizeFxCandles', () => {
  it('把 /chart 的 Decimal-as-float 行归一化为 number 并按时间排序', () => {
    const points = normalizeFxCandles([
      { bucket_start: '2026-01-01T00:01:00Z', interval: '1m', open: '1.10', high: 1.2, low: 1, close: 1.15, volume: '5' },
      { bucket_start: '2026-01-01T00:00:00Z', interval: '1m', open: 1, high: 1.1, low: 0.9, close: 1.05, volume: 3 },
    ])
    expect(points).toEqual([
      { t: '2026-01-01T00:00:00Z', o: 1, h: 1.1, l: 0.9, c: 1.05, v: 3 },
      { t: '2026-01-01T00:01:00Z', o: 1.1, h: 1.2, l: 1, c: 1.15, v: 5 },
    ])
    expect(normalizeFxCandles(null)).toEqual([])
  })
})

describe('错误映射', () => {
  it('按后端 detail 映射玩家可读文案', () => {
    expect(mapFxError({ status: 403, data: { detail: 'FX trading is disabled' } })).toContain('总闸')
    expect(
      mapFxError({ status: 409, data: { detail: 'quoted output is below min_out' } }),
    ).toContain('滑点')
    expect(mapFxError({ status: 400, data: { detail: 'insufficient cash' } })).toContain('余额不足')
    expect(
      mapFxError({ status: 403, data: { detail: 'outstanding debt blocks FX purchases' } }),
    ).toContain('借款')
    expect(mapFxError({ status: 404 })).toContain('不存在')
    expect(mapFxError({ status: 500 }, '兜底')).toBe('兜底')
  })

  it('409 判定用于触发 snapshot 刷新', () => {
    expect(isConflictError({ status: 409, data: { detail: 'x' } })).toBe(true)
    expect(isConflictError({ status: 400 })).toBe(false)
    expect(isConflictError(null)).toBe(false)
  })
})

describe('幂等键', () => {
  it('每笔成交生成不同的非空键', () => {
    const a = newFxIdempotencyKey()
    const b = newFxIdempotencyKey()
    expect(a.length).toBeGreaterThan(8)
    expect(a).not.toBe(b)
  })
})

// ── Fix round C1/I1/I2 行为测试 ──

const draftAdmin: FxPairAdmin = {
  id: 7,
  currency_code: 'CCB',
  currency_name: '幻想币',
  status: 'draft',
  pool_version: 1,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  gold_reserve: '1000',
  foreign_reserve: '1000',
  target_price: '1',
  initial_price: '1',
  target_min: '0.5',
  target_max: '2',
  buy_fee_rate: '0.002',
  sell_fee_rate: '0.002',
}

function publicOf(pair: FxPairAdmin): FxPairPublic {
  return {
    id: pair.id,
    currency_code: pair.currency_code,
    currency_name: pair.currency_name,
    status: pair.status,
    pool_version: pair.pool_version,
    created_at: pair.created_at,
    updated_at: pair.updated_at,
  }
}

describe('C1 管理端本地 pair 合并：草稿可进入 fund/open 流程', () => {
  it('公开列表过滤 draft，本地合并后仍包含草稿并可开市', () => {
    let store: Record<number, FxPairAdmin> = {}
    store = upsertFxPairAdmin(store, draftAdmin)

    // 后端 GET /fx/pairs 过滤 draft → 公开列表为空，但本地可见
    let visible = mergeFxPairs(store, [])
    expect(visible.map((p) => p.id)).toEqual([7])
    expect(visible[0]!.status).toBe('draft')

    // 注资：同 id 返回更新后的管理员记录，草稿仍在列表中
    store = upsertFxPairAdmin(store, { ...draftAdmin, gold_reserve: '1500' })
    visible = mergeFxPairs(store, [])
    expect(visible).toHaveLength(1)
    expect(visible[0]!.status).toBe('draft')

    // 开市状态转换：patch 返回 trading，本地列表随之更新
    store = upsertFxPairAdmin(store, { ...draftAdmin, status: 'trading', gold_reserve: '1500' })
    visible = mergeFxPairs(store, [])
    expect(visible[0]!.status).toBe('trading')

    // 公开列表随后出现同一 pair 时按 id 去重，公开共享字段为准
    const publicTrading: FxPairPublic = { ...publicOf(draftAdmin), status: 'paused' }
    const merged = mergeFxPairs(store, [publicTrading])
    expect(merged).toHaveLength(1)
    expect(merged[0]!.status).toBe('paused')
  })
})

describe('I1 并发 submit 单飞', () => {
  it('并发调用只触发一次 API，第二次返回 null', async () => {
    const submitter = new FxOrderSubmitter()
    let calls = 0
    const run = async (key: string) => {
      calls += 1
      expect(key).toMatch(/^fx-/)
      await Promise.resolve()
      return 'ok'
    }
    const [a, b] = await Promise.all([
      submitter.submit('sig', run),
      submitter.submit('sig', run),
    ])
    expect(calls).toBe(1)
    expect(a).toBe('ok')
    expect(b).toBeNull()
    // 完成后可再次提交
    expect(await submitter.submit('sig', run)).toBe('ok')
    expect(calls).toBe(2)
  })
})

describe('I2 逻辑订单幂等键复用', () => {
  it('同一签名复用同一键，参数变化生成新键，成功后 reset', () => {
    const submitter = new FxOrderSubmitter()
    const sig = fxOrderSignature({ pairId: 1, side: 'buy', amount: '10', minOut: '9.900000' })
    const first = submitter.keyFor(sig)
    expect(submitter.keyFor(sig)).toBe(first) // 重试/409 后参数一致 → 复用

    const changed = fxOrderSignature({ pairId: 1, side: 'buy', amount: '11', minOut: '10.890000' })
    expect(submitter.keyFor(changed)).not.toBe(first)

    submitter.reset()
    expect(submitter.keyFor(changed)).not.toBe(first)
  })
})
