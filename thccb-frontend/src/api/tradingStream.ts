import { useAuthStore } from '@/stores/auth'
import { apiBaseUrl, authIdentityGuard, refreshAccessToken } from './index'
import { groupQuery } from './tradingContext'
import type { GroupKey } from '@/types/tradingContext'
export interface StreamFrame { event: string; data: string; id?: string }
export interface TradingStreamHandlers { frame: (frame: StreamFrame) => void; disconnected?: () => void; error?: (error: unknown) => void }
/** Incremental SSE framing. TextDecoder retains split UTF-8 codepoints. */
export async function readSse(body: ReadableStream<Uint8Array>, receive: (frame: StreamFrame) => void, signal?: AbortSignal) {
  const reader = body.getReader(), decoder = new TextDecoder()
  let buffer = '', event = '', id: string | undefined, data: string[] = []
  const line = (value: string) => {
    if (!value) {
      if (data.length) receive({ event: event || 'message', data: data.join('\n'), id })
      event = ''; data = []; return
    }
    if (value.startsWith(':')) return
    const colon = value.indexOf(':'), field = colon < 0 ? value : value.slice(0, colon)
    let content = colon < 0 ? '' : value.slice(colon + 1)
    if (content.startsWith(' ')) content = content.slice(1)
    if (field === 'event') event = content
    else if (field === 'data') data.push(content)
    else if (field === 'id' && !content.includes('\0')) id = content
  }
  const consume = (final = false) => {
    while (buffer.length) {
      const index = buffer.search(/[\r\n]/)
      if (index < 0 || !final && buffer[index] === '\r' && index === buffer.length - 1) break
      const size = buffer[index] === '\r' && buffer[index + 1] === '\n' ? 2 : 1
      line(buffer.slice(0, index)); buffer = buffer.slice(index + size)
    }
  }
  const abort = () => { void reader.cancel() }
  signal?.addEventListener('abort', abort, { once: true })
  try {
    while (!signal?.aborted) {
      const result = await reader.read()
      if (signal?.aborted) break
      if (result.done) { buffer += decoder.decode(); consume(true); break }
      buffer += decoder.decode(result.value, { stream: true }); consume()
    }
  } finally { signal?.removeEventListener('abort', abort); reader.releaseLock() }
}
export class TradingStream {
  private controller: AbortController | null = null
  private timer: ReturnType<typeof setTimeout> | null = null
  close() { this.controller?.abort(); this.controller = null; if (this.timer) clearTimeout(this.timer); this.timer = null }
  connect(group: GroupKey | null, handlers: TradingStreamHandlers) {
    this.close()
    const controller = new AbortController(), valid = authIdentityGuard()
    this.controller = controller
    let failures = 0
    const active = () => this.controller === controller && !controller.signal.aborted && valid()
    const run = async () => {
      try {
        const auth = useAuthStore()
        const url = `${apiBaseUrl.replace(/\/$/, '')}/api/v1/stream/trading?${groupQuery(group)}`
        const request = () => fetch(url, { headers: { Authorization: `Bearer ${auth.accessToken}`, Accept: 'text/event-stream' }, signal: controller.signal })
        let response = await request()
        if (!active()) { void response.body?.cancel(); return }
        if (response.status === 401) {
          await refreshAccessToken(valid)
          if (!active()) return
          response = await request()
        }
        if (response.status === 401) { if (active()) auth.logout(); return }
        if (response.status === 403) {
          const payload = await response.json().catch(() => null)
          if (active() && payload?.detail === 'USER_BANNED' && typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('user-banned'))
          if (active()) { handlers.disconnected?.(); this.close() }
          return
        }
        if (!response.ok || !response.body) throw new Error(`Trading stream HTTP ${response.status}`)
        if (!active()) { void response.body.cancel(); return }
        await readSse(response.body, frame => { if (active()) { failures = 0; handlers.frame(frame) } }, controller.signal)
        if (active()) handlers.disconnected?.()
      } catch (error) { if (active()) { handlers.disconnected?.(); handlers.error?.(error) } }
      if (active()) this.timer = setTimeout(() => { void run() }, Math.min(15000, 1000 * 2 ** Math.min(failures++, 4)))
    }
    void run()
  }
}
