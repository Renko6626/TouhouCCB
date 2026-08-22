import type { MarketEvent } from '@/types/api'

export class MarketStream {
  private eventSource: EventSource | null = null
  private marketId: number | null = null
  private reconnectAttempts = 0
  // 无限重连 + 指数退避（1s, 2s, 4s … 封顶 30s）+ jitter。
  // 此前上限 5 次 / 累计约 15s：每次 push main 部署（stop_grace 8s + 重启 + alembic）
  // 都超过这个窗口，所有在线用户从此行情冻结、本地报价用旧价，且没有任何再触发路径。
  private reconnectBaseDelay = 1000
  private reconnectMaxDelay = 30000
  // 重连 generation token —— 每次 connect/disconnect/_openConnection 递增。
  // setTimeout 重连 fire 时对比 token，如果期间状态变了就放弃这次重连，
  // 防止"setTimeout 还没 fire，用户已经 disconnect + reconnect"造成的双 EventSource。
  private reconnectGen = 0
  private listeners: Map<string, Set<(data: any) => void>> = new Map()

  constructor() {
    this.listeners.set('open', new Set())
    this.listeners.set('snapshot', new Set())
    this.listeners.set('trade', new Set())
    this.listeners.set('market_status', new Set())
    this.listeners.set('ping', new Set())
    this.listeners.set('tick', new Set())
    this.listeners.set('error', new Set())
  }

  connect(marketId: number) {
    if (this.eventSource && this.marketId === marketId) {
      if (import.meta.env.DEV) console.log(`Already connected to market ${marketId}`)
      return
    }
    // 切到新市场：重置重连计数
    this.disconnect()
    this._openConnection(marketId)
  }

  // 实际打开 EventSource —— 被 connect() 和 onerror 重连用。
  // 不重置 reconnectAttempts，让重连流程能正确累加。
  private _openConnection(marketId: number) {
    this.reconnectGen++  // 任何新连接都让"已调度的重连 setTimeout"失效
    this.marketId = marketId
    const baseUrl = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8004'
    const normalizedBaseUrl = baseUrl.replace(/\/$/, '')
    const url = `${normalizedBaseUrl}/api/v1/stream/market/${marketId}`

    try {
      this.eventSource = new EventSource(url)
      this.setupEventHandlers()
      if (import.meta.env.DEV) console.log(`Opening market stream: ${marketId}`)
    } catch (error) {
      console.error('Failed to open market stream:', error)
      this.handleError(error)
    }
  }

  /** 运行时校验 SSE 事件结构是否合法 */
  private isValidMarketEvent(data: unknown): data is MarketEvent {
    if (typeof data !== 'object' || data === null) return false
    const d = data as Record<string, unknown>
    return (
      typeof d.market_id === 'number' &&
      typeof d.ts === 'string' &&
      ['snapshot', 'trade', 'market_status', 'ping', 'tick'].includes(d.type as string)
    )
  }

  private setupEventHandlers() {
    if (!this.eventSource) return

    const parsePayload = (raw: string): MarketEvent | null => {
      try {
        const parsed = JSON.parse(raw)
        if (!this.isValidMarketEvent(parsed)) {
          console.warn('[SSE] 收到非法事件格式:', parsed)
          return null
        }
        return parsed
      } catch (error) {
        console.error('[SSE] JSON 解析失败:', error, raw)
        return null
      }
    }

    const handleNamedEvent = (eventType: MarketEvent['type']) => (event: MessageEvent) => {
      const parsed = parsePayload(event.data)
      if (!parsed) return
      this.handleEvent({ ...parsed, type: eventType })
    }

    // 后端使用命名 SSE event: trade / market_status / snapshot / ping
    this.eventSource.addEventListener('snapshot', handleNamedEvent('snapshot'))
    this.eventSource.addEventListener('trade', handleNamedEvent('trade'))
    this.eventSource.addEventListener('market_status', handleNamedEvent('market_status'))
    this.eventSource.addEventListener('ping', handleNamedEvent('ping'))
    this.eventSource.addEventListener('tick', handleNamedEvent('tick'))

    this.eventSource.onmessage = (event) => {
      const data = parsePayload(event.data)
      if (!data) return
      this.handleEvent(data)
    }

    this.eventSource.onerror = (error) => {
      console.error('EventSource error:', error)
      this.handleError(error)

      // 立即关掉当前 ES — 否则浏览器自带的自动重连和我们下面的 setTimeout 重连会并发，
      // 导致连接风暴 + reconnectAttempts 误计。
      // 注意只清 eventSource，不清 marketId / reconnectAttempts（那是 disconnect 的事）。
      if (this.eventSource) {
        this.eventSource.close()
        this.eventSource = null
      }

      if (this.marketId === null) return

      this.reconnectAttempts++
      const targetMarketId = this.marketId
      const targetGen = this.reconnectGen
      // jitter ±30%：防止 N 个客户端同时断开后同步重连打 backend（thundering herd）
      const base = Math.min(
        this.reconnectBaseDelay * 2 ** (this.reconnectAttempts - 1),
        this.reconnectMaxDelay,
      )
      const delay = base * (0.7 + Math.random() * 0.6)
      setTimeout(() => {
        // 期间用户 disconnect / 切换市场 / 已重连 → 放弃这次定时重连
        if (this.reconnectGen !== targetGen) return
        if (this.marketId !== targetMarketId) return
        if (import.meta.env.DEV) console.log(`Reconnecting... attempt ${this.reconnectAttempts}`)
        this._openConnection(targetMarketId)
      }, delay)
    }

    this.eventSource.onopen = () => {
      if (import.meta.env.DEV) console.log('EventSource connection opened')
      this.reconnectAttempts = 0
      this.emit('open', { market_id: this.marketId })
    }
  }

  private handleEvent(event: MarketEvent) {
    this.emit(event.type, event)
  }

  private handleError(error: any) {
    this.emit('error', { type: 'connection_error', error })
  }

  on(eventType: string, callback: (data: any) => void) {
    if (!this.listeners.has(eventType)) {
      this.listeners.set(eventType, new Set())
    }
    this.listeners.get(eventType)!.add(callback)
  }

  off(eventType: string, callback: (data: any) => void) {
    if (this.listeners.has(eventType)) {
      this.listeners.get(eventType)!.delete(callback)
    }
  }

  private emit(eventType: string, data: any) {
    if (this.listeners.has(eventType)) {
      this.listeners.get(eventType)!.forEach(callback => {
        try {
          callback(data)
        } catch (error) {
          console.error(`Error in ${eventType} listener:`, error)
        }
      })
    }
  }

  /** 断线状态下立即重连（跳过退避）——给 tab 回前台 / navigator online 事件用。
   *  已连接或从未 connect 过则 no-op。 */
  reconnectNow() {
    if (this.marketId === null || this.eventSource !== null) return
    this.reconnectAttempts = 0
    this._openConnection(this.marketId)
  }

  disconnect() {
    this.reconnectGen++  // 让所有已调度的重连 setTimeout 失效
    if (this.eventSource) {
      this.eventSource.close()
      this.eventSource = null
      if (import.meta.env.DEV) console.log('Disconnected from market stream')
    }
    this.marketId = null
    // 主动断开 = 干净状态，复位重连计数（防止下次连新市场带着旧计数）
    this.reconnectAttempts = 0
  }

  get isConnected(): boolean {
    return this.eventSource !== null && this.eventSource.readyState === EventSource.OPEN
  }

  get currentMarketId(): number | null {
    return this.marketId
  }

  get reconnectCount(): number {
    return this.reconnectAttempts
  }
}
