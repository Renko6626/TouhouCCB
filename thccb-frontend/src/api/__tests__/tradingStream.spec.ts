import axios from 'axios'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { readSse, TradingStream } from '../tradingStream'
import { refreshAccessToken } from '../index'
const auth = vi.hoisted(() => ({ user: { id: 1 }, accessToken: 'A', refreshToken: 'R', sessionGeneration: 1, logout: vi.fn() }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))
const streams: TradingStream[] = []
function stream() { const value = new TradingStream(); streams.push(value); return value }
beforeEach(() => { vi.useFakeTimers(); auth.user = { id: 1 }; auth.accessToken = 'A'; auth.refreshToken = 'R'; auth.sessionGeneration = 1; vi.stubGlobal('localStorage', { setItem: vi.fn() }) })
afterEach(() => { streams.splice(0).forEach(value => value.close()); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
it('decodes split UTF-8, multiline data, named events, CRLF, ids and heartbeat comments', async () => {
  const bytes = new TextEncoder().encode(': heartbeat\r\nevent: rules\r\nid: 7\r\ndata: {"text": "中文",\r\ndata: "ready": true}\r\n\r\nevent: heartbeat\ndata: {}\n\n')
  const body = new ReadableStream<Uint8Array>({ start(controller) { for (const byte of bytes) controller.enqueue(Uint8Array.of(byte)); controller.close() } })
  const receive = vi.fn(); await readSse(body, receive)
  expect(receive.mock.calls.map(([value]) => value)).toEqual([{ event: 'rules', id: '7', data: '{"text": "中文",\n"ready": true}' }, { event: 'heartbeat', id: '7', data: '{}' }])
})
it('authenticates via header and stops reader delivery/reconnect on close', async () => {
  const cancel = vi.fn()
  const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new TextEncoder().encode('event: sync\ndata: {}\n\n')) }, cancel })
  const fetcher = vi.fn().mockResolvedValue(new Response(body, { status: 200 }))
  vi.stubGlobal('fetch', fetcher)
  const frames = vi.fn(), value = stream(); value.connect({ product: 'fx', id: 5 }, { frame: frames })
  await vi.advanceTimersByTimeAsync(0)
  expect(fetcher.mock.calls[0]![0]).toContain('current_id=5')
  expect(fetcher.mock.calls[0]![0]).not.toContain('token')
  expect(fetcher.mock.calls[0]![1].headers.Authorization).toBe('Bearer A')
  expect(frames).toHaveBeenCalledTimes(1)
  value.close(); await vi.advanceTimersByTimeAsync(30000)
  expect(cancel).toHaveBeenCalledTimes(1); expect(fetcher).toHaveBeenCalledTimes(1)
})
it('renews once with the shared HTTP singleflight and reconnects to a fresh sync window', async () => {
  let finish!: (value: { data: { access_token: string } }) => void
  const renewal = vi.spyOn(axios, 'post').mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const fetcher = vi.fn().mockResolvedValueOnce(new Response('', { status: 401 })).mockResolvedValueOnce(new Response('event: sync\ndata: {}\n\n')).mockResolvedValue(new Response('event: sync\ndata: {}\n\n'))
  vi.stubGlobal('fetch', fetcher)
  const frames = vi.fn(), disconnected = vi.fn()
  stream().connect(null, { frame: frames, disconnected })
  await vi.advanceTimersByTimeAsync(0)
  const httpRenewal = refreshAccessToken()
  finish({ data: { access_token: 'new-A' } }); await httpRenewal; await vi.advanceTimersByTimeAsync(0)
  expect(renewal).toHaveBeenCalledTimes(1)
  expect(fetcher.mock.calls[1]![1].headers.Authorization).toBe('Bearer new-A')
  expect(disconnected).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(1000)
  expect(frames).toHaveBeenCalledTimes(2)
})
it('discards delayed stream and renewal responses from an earlier login generation', async () => {
  let finish!: (value: Response) => void
  const fetcher = vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve }))
  vi.stubGlobal('fetch', fetcher)
  const frames = vi.fn(); stream().connect(null, { frame: frames })
  auth.sessionGeneration++; auth.user = { id: 2 }; auth.accessToken = 'B'; auth.refreshToken = 'RB'
  finish(new Response('event: account_changed\ndata: {}\n\n')); await vi.advanceTimersByTimeAsync(20000)
  expect(frames).not.toHaveBeenCalled(); expect(fetcher).toHaveBeenCalledTimes(1)
})
