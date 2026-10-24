import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAuthStore } from '@/stores/auth'
import type { User } from '@/types/api'
import { fetchSiteResource, openSiteEventSource } from '../siteTransport'

beforeEach(() => {
  vi.stubGlobal('localStorage', {
    getItem: vi.fn().mockReturnValue(null),
    setItem: vi.fn(),
    removeItem: vi.fn(),
  })
  setActivePinia(createPinia())
  const auth = useAuthStore()
  auth.user = { id: 1, is_superuser: true } as User
  auth.accessToken = 'admin-access'
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('attaches administrator identity to existing public chart/history requests', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response('{"points":[]}'))
  vi.stubGlobal('fetch', fetcher)
  const response = await fetchSiteResource('/history/fx/1/version/1m/0.json')
  expect(await response.json()).toEqual({ points: [] })
  expect(fetcher.mock.calls[0]![1].headers.get('Authorization')).toBe('Bearer admin-access')
})

it('delivers named public SSE frames through the authenticated existing reader', async () => {
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({
    start(value) {
      controller = value
    },
  })
  const fetcher = vi.fn().mockResolvedValue(new Response(body))
  vi.stubGlobal('fetch', fetcher)
  const source = openSiteEventSource('/api/v1/fx/stream/1')
  const received = new Promise<MessageEvent>((resolve) => source.addEventListener('fx', resolve))
  try {
    controller.enqueue(new TextEncoder().encode('event: fx\ndata: {"price":"5.0"}\n\n'))
    expect((await received).data).toBe('{"price":"5.0"}')
    expect(fetcher.mock.calls[0]![1].headers.Authorization).toBe('Bearer admin-access')
    expect(source.readyState).toBe(1)
  } finally {
    source.close()
  }
  expect(source.readyState).toBe(2)
})
