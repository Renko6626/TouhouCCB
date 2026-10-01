// FX Task 8 行为测试：金额格式化、min-out/滑点、SSE 公开帧白名单、错误映射。
// 断言的是可执行行为，不是源码字符串或恒真 identity。
import { describe, expect, it } from 'vitest'
import {
  FxPendingShortOrder,
  computeMaxGoldIn,
  computeMinOut,
  divideFxAmount,
  expandExponential,
  formatFxAmount,
  formatFxPrice,
  FxOrderSubmitter,
  fxOrderSignature,
  fxPriceDecimals,
  fxPricePrecision,
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
    ).toContain('最低可接受金额')
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

// ── FX 高敏感汇率精度：动态小数位，价格 formatter 与金额 formatter 分离 ──

describe('FX 价格精度（动态小数位）', () => {
  it('0.2 / 12.345678 默认 6 位，不再固定 2 位', () => {
    expect(fxPriceDecimals('0.2')).toBe(6)
    expect(formatFxPrice('0.2')).toBe('0.200000')
    expect(fxPriceDecimals('12.345678')).toBe(6)
    expect(formatFxPrice('12.345678')).toBe('12.345678')
    expect(formatFxPrice('12345.6')).toBe('12345.600000')
  })

  it('0.000123 一类极小汇率按数量级扩展小数位', () => {
    expect(fxPriceDecimals('0.000123')).toBe(9)
    expect(formatFxPrice('0.000123')).toBe('0.000123000')
    // 1.23e-7：目标 6 位有效数字 → 14，触顶 12
    expect(fxPriceDecimals('0.000000123')).toBe(12)
    expect(formatFxPrice('0.000000123')).toBe('0.000000123000')
  })

  it('价格 formatter 与金额 formatter 分离：金额仍默认 6 位', () => {
    expect(formatFxAmount('0.000123')).toBe('0.000123')
    expect(formatFxPrice('0.000123')).toBe('0.000123000')
    expect(formatFxAmount('0.2')).toBe('0.200000')
  })

  it('minMove 默认 1e-6，极小汇率才细化', () => {
    expect(fxPricePrecision('0.2')).toEqual({ precision: 6, minMove: 1e-6 })
    expect(fxPricePrecision('12.345678')).toEqual({ precision: 6, minMove: 1e-6 })
    expect(fxPricePrecision('0.000123').precision).toBe(9)
    expect(fxPricePrecision('0.000123').minMove).toBe(1e-9)
  })

  it('NaN / 非法 / 零 / 空返回默认位数或占位符', () => {
    expect(fxPriceDecimals(null)).toBe(6)
    expect(fxPriceDecimals(undefined)).toBe(6)
    expect(fxPriceDecimals('')).toBe(6)
    expect(fxPriceDecimals('abc')).toBe(6)
    expect(fxPriceDecimals(Number.NaN)).toBe(6)
    expect(fxPriceDecimals('0')).toBe(6)
    expect(formatFxPrice('abc')).toBe('—')
    expect(formatFxPrice(null)).toBe('—')
    expect(formatFxPrice('0')).toBe('0.000000')
  })

  it('half-up 舍入的向下/向上边界', () => {
    expect(formatFxPrice('1.2345644')).toBe('1.234564')
    expect(formatFxPrice('1.2345649')).toBe('1.234565')
    expect(formatFxPrice('0.1999995')).toBe('0.200000')
    expect(formatFxPrice('9.9999995')).toBe('10.000000')
    expect(formatFxPrice('-1.2345675')).toBe('-1.234568')
  })

  it('科学计数法 number 在适配层还原后再格式化，不丢展示', () => {
    expect(expandExponential('1e-7')).toBe('0.0000001')
    expect(expandExponential('1.23e+3')).toBe('1230')
    expect(expandExponential('abc')).toBe('abc')
    expect(formatFxAmount(1e-7)).toBe('0.000000')
    expect(formatFxPrice(1e-7)).toBe('0.000000100000')
  })
})

describe('divideFxAmount（平均成本等派生比值不丢精度）', () => {
  it('十进制字符串相除并截断到指定小数位', () => {
    expect(divideFxAmount('100', '4', 12)).toBe('25.000000000000')
    expect(divideFxAmount('1', '3', 6)).toBe('0.333333')
    expect(divideFxAmount('0.123456', '0.2', 6)).toBe('0.617280')
    expect(divideFxAmount('0', '5', 6)).toBe('0.000000')
  })

  it('除零 / 非法输入返回 null', () => {
    expect(divideFxAmount('1', '0')).toBeNull()
    expect(divideFxAmount('abc', '1')).toBeNull()
    expect(divideFxAmount('1', null)).toBeNull()
  })
})

it('开空参考敞口按完整汇率换算数量，不把极小正汇率截成零', () => {
  expect(divideFxAmount('100', '8.200000000000', 6)).toBe('12.195121')
  expect(divideFxAmount('100', '0.000001500000', 6)).toBe('66666666.666666')
  expect(divideFxAmount('100', '0.000000010000', 6)).toBe('10000000000.000000')
  expect(divideFxAmount('100', '0', 6)).toBeNull()
})

describe('交易面板回归：min-out 与双击单飞', () => {
  it('min-out 不放宽滑点保护：随 bps 单调不增且向下取 6 位', () => {
    const output = '0.123456'
    expect(computeMinOut(output, 0)).toBe('0.123456')
    // 0.123456 × 0.995 = 0.12283872 → 向下 0.122838
    expect(computeMinOut(output, 50)).toBe('0.122838')
    const a = computeMinOut(output, 50)
    const b = computeMinOut(output, 100)
    expect(Number(a)).toBeLessThanOrEqual(Number(output))
    expect(Number(b)).toBeLessThanOrEqual(Number(a))
    // 向下取整：0.123456 × 0.9999 = 0.12344365… → 0.123443
    expect(computeMinOut(output, 1)).toBe('0.123443')
  })

  it('报价 await 期间双击只提交一笔 API', async () => {
    const submitter = new FxOrderSubmitter()
    const calls: string[] = []
    const click = async () => {
      let quote = ''
      const fetchQuote = async () => {
        await Promise.resolve()
        quote = '0.900000'
        return quote
      }
      if (!quote) await fetchQuote()
      const signature = fxOrderSignature({ pairId: 1, side: 'buy', amount: '10', minOut: quote })
      return submitter.submit(signature, async () => {
        calls.push(quote)
        await Promise.resolve()
        return 'filled'
      })
    }
    const [first, second] = await Promise.all([click(), click()])
    expect(calls).toHaveLength(1)
    expect([first, second].filter((r) => r === 'filled')).toHaveLength(1)
    expect([first, second].filter((r) => r === null)).toHaveLength(1)
  })
})

describe('cover maximum gold input', () => {
  it('ceilings sub-unit tolerances and preserves large decimal amounts', () => {
    expect(computeMaxGoldIn('0.000001', 50)).toBe('0.000002')
    expect(computeMaxGoldIn('1.000001', 50)).toBe('1.005002')
    expect(computeMaxGoldIn('123456789012345678.123456', 50)).toBe('124074072957407406.514074')
    expect(computeMaxGoldIn('1.0000001', 0)).toBe('1.000001')
    expect(computeMaxGoldIn('2', 100)).toBe('2.020000')
  })
})

describe('ambiguous short response retries', () => {
  function storage() {
    const values = new Map<string, string>()
    return {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value) },
      removeItem: (key: string) => { values.delete(key) },
    }
  }

  it.each(['open', 'cover'] as const)('replays identical %s request after a committed response is lost', async action => {
    const saved = storage()
    const order = new FxPendingShortOrder(42, saved)
    const calls: unknown[] = []
    const executed = new Set<string>()
    const run = async (request: { body: { idempotency_key: string } }) => {
      calls.push(request)
      executed.add(request.body.idempotency_key)
      if (calls.length === 1) throw new Error('response lost')
      return { replay: true }
    }
    const body = action === 'open' ? { foreign_amount: '3.123456', min_gold_out: '4.000001' }
      : { foreign_amount: '1.000001', max_gold_in: '2.123456' }
    await expect(order.start(7, action, body, run)).rejects.toThrow('response lost')
    await expect(order.start(7, action, body, run)).rejects.toThrow()
    expect(calls).toHaveLength(1)
    const remounted = new FxPendingShortOrder(42, saved)
    expect(remounted.pending).toEqual(calls[0])
    expect(new FxPendingShortOrder(43, saved).pending).toBeNull()
    await expect(remounted.start(7, action, body, run)).rejects.toThrow()
    expect(await remounted.retry(run)).toEqual({ replay: true })
    expect(calls[1]).toEqual(calls[0])
    expect(executed.size).toBe(1)
    expect(new FxPendingShortOrder(42, saved).pending).toBeNull()
  })
})

it('retains ambiguous server failures but releases a definitively rejected short request', async () => {
  const saved = new Map<string, string>()
  const storage = {
    getItem: (key: string) => saved.get(key) ?? null,
    setItem: (key: string, value: string) => { saved.set(key, value) },
    removeItem: (key: string) => { saved.delete(key) },
  }
  const order = new FxPendingShortOrder(42, storage)
  await expect(order.start(7, 'open', { foreign_amount: '1', min_gold_out: '2' }, async () => { throw { response: { status: 503 } } })).rejects.toEqual({ response: { status: 503 } })
  expect(order.pending).not.toBeNull()
  await expect(order.retry(async () => { throw { status: 422 } })).rejects.toEqual({ status: 422 })
  expect(order.pending).toBeNull()
  expect(new FxPendingShortOrder(42, storage).pending).toBeNull()
})

it('never sends a short write when its retry identity cannot be persisted', async () => {
  const storage = {
    getItem: () => null,
    setItem: () => { throw new Error('quota') },
    removeItem: () => {},
  }
  const order = new FxPendingShortOrder(42, storage)
  let writes = 0
  await expect(order.start(7, 'open', { foreign_amount: '1', min_gold_out: '2' }, async () => { writes++; return {} }))
    .rejects.toThrow('Cannot safely persist')
  expect(writes).toBe(0)
  expect(order.hasUnresolved).toBe(true)
})

it('a late response from an unmounted page cannot erase a newer pending short', async () => {
  const values = new Map<string, string>()
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
    removeItem: (key: string) => { values.delete(key) },
  }
  const oldPage = new FxPendingShortOrder(42, storage)
  let finishOld!: (value: string) => void
  const oldResponse = oldPage.start(7, 'open', { foreign_amount: '1.', min_gold_out: '2' },
    () => new Promise<string>(resolve => { finishOld = resolve }))
  const remounted = new FxPendingShortOrder(42, storage)
  expect(remounted.pending?.body.foreign_amount).toBe('1.')
  expect(await remounted.retry(async () => 'old replay')).toBe('old replay')
  await expect(remounted.start(7, 'open', { foreign_amount: '3', min_gold_out: '4' },
    async () => { throw new Error('new response lost') })).rejects.toThrow('new response lost')
  const newKey = remounted.pending?.body.idempotency_key
  expect(newKey).toBeTruthy()
  finishOld('old committed')
  expect(await oldResponse).toBe('old committed')
  expect(new FxPendingShortOrder(42, storage).pending?.body.idempotency_key).toBe(newKey)
})
