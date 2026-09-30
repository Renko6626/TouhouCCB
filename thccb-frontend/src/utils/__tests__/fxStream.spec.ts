import { afterEach, describe, expect, it, vi } from 'vitest'
import { FxStream } from '@/api/fx'

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
