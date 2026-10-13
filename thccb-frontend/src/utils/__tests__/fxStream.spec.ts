import { afterEach, describe, expect, it, vi } from 'vitest'
import { FxStream, parseFxSseEnvelope } from '@/api/fx'

class MockSource {
  static OPEN = 1
  static instances: MockSource[] = []
  listeners = new Map<string, (event: { data: string }) => void>()
  onopen: (() => void) | null = null
  onerror: ((error: unknown) => void) | null = null
  readyState = 1
  constructor(public url: string) { MockSource.instances.push(this) }
  addEventListener(name: string, callback: (event: { data: string }) => void) { this.listeners.set(name, callback) }
  close() { this.readyState = 2 }
  emit(name: string, data: unknown) { this.listeners.get(name)?.({ data: JSON.stringify(data) }) }
}

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); MockSource.instances = [] })

describe('FX 实时连接', () => {
  it('接收首连 / 重连 snapshot，同时忽略已切换币种的迟到帧和错误', () => {
    vi.stubGlobal('EventSource', MockSource)
    const stream = new FxStream()
    const onFrame = vi.fn()
    const onError = vi.fn()
    stream.onFrame(onFrame)
    stream.onError(onError)
    stream.connect(1)
    const first = MockSource.instances[0]!
    first.emit('snapshot', { price: '1.2' })
    expect(onFrame).toHaveBeenCalledWith({ price: '1.2' })
    stream.connect(2)
    first.emit('fx', { price: '999' })
    first.onerror?.(new Error('old stream'))
    expect(onFrame).toHaveBeenCalledTimes(1)
    expect(onError).not.toHaveBeenCalled()
    MockSource.instances[1]!.emit('fx', { price: '2.3' })
    expect(onFrame).toHaveBeenLastCalledWith({ price: '2.3' })
    stream.disconnect()
  })

  it('手动重连清理旧重试计时器，不再重复创建连接', () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', MockSource)
    const stream = new FxStream()
    stream.connect(1)
    MockSource.instances[0]!.onerror?.(new Error('offline'))
    stream.reconnectNow()
    vi.advanceTimersByTime(35000)
    expect(MockSource.instances).toHaveLength(2)
    stream.disconnect()
  })
})

describe('FX envelope 解析', () => {
  it('envelope 暴露历史版本/尾段/逐笔成交，onFrame 仍只返回报价白名单', () => {
    vi.stubGlobal('EventSource', MockSource)
    const stream = new FxStream()
    const onFrame = vi.fn()
    const onEnvelope = vi.fn()
    stream.onFrame(onFrame)
    stream.onEnvelope(onEnvelope)
    stream.connect(7)
    MockSource.instances[0]!.emit('snapshot', {
      price: '1.28',
      // 私有/隐藏字段：绝不能进入任何监听器
      gold_reserve: '999',
      shock_ratio: '0.1',
      future_orders: [{ side: 'buy' }],
      random_state: 42,
      parameter_snapshot: { secret: true },
      history_ready: true,
      history_version: '11111111-1111-1111-1111-111111111111',
      history_tail: {
        '1m': {
          t0: 1755734400, step: 60, n_buckets: 3600,
          t: [0], o: ['1.25000000'], h: ['1.30000000'], l: ['1.20000000'],
          c: ['1.28000000'], v: ['12.500000'], trades: [3], events: [4],
        },
      },
      history_tail_at: '2026-01-01T00:00:00.000Z',
      history_tail_through_event_id: 42,
      events: [{ n_trades: 1, id: 43, ts: '2026-01-01T00:01:00.000Z', pre_price: '1.250000', post_price: '1.29000000', gold_volume: '5.000000' }],
      history_invalidated: true,
    })

    expect(onFrame).toHaveBeenCalledWith({ price: '1.28' })
    const env = onEnvelope.mock.calls[0]![0]
    expect(env.price).toBe('1.28')
    expect(env.history_ready).toBe(true)
    expect(env.history_version).toBe('11111111-1111-1111-1111-111111111111')
    expect(env.history_tail?.['1m']?.o).toEqual(['1.25000000'])
    expect(env.history_tail_through_event_id).toBe(42)
    expect(env.events?.[0]).toMatchObject({ id: 43, pre_price: '1.250000', post_price: '1.29000000', gold_volume: '5.000000' })
    expect(env.history_invalidated).toBe(true)
    expect('gold_reserve' in env).toBe(false)
    expect('shock_ratio' in env).toBe(false)
    expect('future_orders' in env).toBe(false)
    expect('random_state' in env).toBe(false)
    stream.disconnect()
  })

  it('结构非法的 history_tail / trades 被丢弃，不影响旧报价帧', () => {
    const raw = JSON.stringify({
      type: 'fx',
      data: {
        price: '1.5',
        history_tail: {
          '1m': { t0: 1, step: 60, n_buckets: 3, t: [0], o: ['1'], h: ['1'], l: ['1'], c: ['1'], v: ['1'] },
        },
        trades: [{ id: 1, ts: 'not-a-date', post_price: '1', gold_volume: '1' }, { id: 2 }],
      },
    })
    const env = parseFxSseEnvelope(raw)
    expect(env?.price).toBe('1.5')
    expect(env?.history_tail).toBeUndefined()
    expect(env?.trades).toBeUndefined()
  })
})


it('public events retain zero volume and strip liquidity/admin details without using trade coverage', () => {
  const env = parseFxSseEnvelope(JSON.stringify({ data: {
    history_tail_through_trade_id: 99,
    history_tail_through_event_id: 7,
    events: [{ id: 8, ts: '2026-01-01T00:01:00Z', pre_price: '1', post_price: '2', gold_volume: '0', n_trades: 0,
      gold_reserve: '100', audit_id: 9, operator_user_id: 1, kind: 'fund' }],
    trades: [{ id: 99, ts: '2026-01-01T00:01:00Z', pre_price: '1', post_price: '2', gold_volume: '10' }],
  } }))!
  expect(env.history_tail_through_event_id).toBe(7)
  expect(env.events).toEqual([{ id: 8, ts: '2026-01-01T00:01:00Z', pre_price: '1', post_price: '2', gold_volume: '0', n_trades: 0 }])
  expect(env).not.toHaveProperty('history_tail_through_trade_id')
})


it('not-ready history keeps status explicit and never invents an event coverage cursor', () => {
  const env = parseFxSseEnvelope(JSON.stringify({ data: { price: '2', history_ready: false, history_invalidated: true,
    history_tail_through_trade_id: 100 } }))!
  expect(env).toMatchObject({ price: '2', history_ready: false, history_invalidated: true })
  expect(env.history_tail_through_event_id).toBeUndefined()
  expect(env.history_tail).toBeUndefined()
})
