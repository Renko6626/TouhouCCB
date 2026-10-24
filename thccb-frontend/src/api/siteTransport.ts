import { getActivePinia } from 'pinia'
import { useAuthStore } from '@/stores/auth'
import { authIdentityGuard, refreshAccessToken } from './index'
import { TradingStream } from './tradingStream'

function currentAuth() {
  return getActivePinia() ? useAuthStore() : null
}

/** Public chart/history requests carry the existing bearer identity when logged in. */
export async function fetchSiteResource(url: string, init: RequestInit = {}) {
  const auth = currentAuth(),
    valid = auth ? authIdentityGuard() : () => true
  const request = () => {
    const headers = new Headers(init.headers)
    if (auth?.accessToken) headers.set('Authorization', `Bearer ${auth.accessToken}`)
    return fetch(url, { ...init, headers })
  }
  let response = await request()
  if (!valid()) {
    void response.body?.cancel()
    throw new Error('登录账号已变化')
  }
  if (response.status === 401 && auth?.refreshToken) {
    await refreshAccessToken(valid)
    response = await request()
  }
  if (valid() && response.status === 403) {
    const payload = await response
      .clone()
      .json()
      .catch(() => null)
    if (payload?.detail === 'SITE_ACCESS_DISABLED')
      window.dispatchEvent(new CustomEvent('site-access-disabled'))
  }
  return response
}

export interface SiteEventSource {
  readonly readyState: number
  onopen: ((event: Event) => void) | null
  onerror: ((event: Event) => void) | null
  onmessage: ((event: MessageEvent) => void) | null
  addEventListener(type: string, listener: (event: MessageEvent) => void): void
  close(): void
}

/** Native EventSource cannot send a bearer header; reuse the existing fetch-SSE client. */
export function openSiteEventSource(url: string): SiteEventSource {
  if (!currentAuth()?.accessToken) return new EventSource(url)
  const stream = new TradingStream()
  const listeners = new Map<string, Set<(event: MessageEvent) => void>>()
  const source: SiteEventSource = {
    readyState: 0,
    onopen: null,
    onerror: null,
    onmessage: null,
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, new Set())
      listeners.get(type)!.add(listener)
    },
    close() {
      stream.close()
      Object.assign(source, { readyState: 2 })
    },
  }
  stream.connectUrl(url, {
    opened() {
      Object.assign(source, { readyState: 1 })
      source.onopen?.(new Event('open'))
    },
    frame(frame) {
      const event = new MessageEvent(frame.event, { data: frame.data, lastEventId: frame.id })
      listeners.get(frame.event)?.forEach((listener) => listener(event))
      if (frame.event === 'message') source.onmessage?.(event)
    },
    disconnected() {
      Object.assign(source, { readyState: 0 })
      source.onerror?.(new Event('error'))
    },
  })
  return source
}
