import type { MarketEvent } from '@/types/api'

export class MarketStream {
  private eventSource: EventSource | null = null
  private marketId: number | null = null
  private reconnectAttempts = 0
  private maxReconnectAttempts = 5
  private reconnectDelay = 1000
  // 重连 generation token —— 每次 connect/disconnect/_openConnection 递增。
  // setTimeout 重连 fire 时对比 token，如果期间状态变了就放弃这次重连，
  // 防止"setTimeout 还没 fire，用户已经 disconnect + reconnect"造成的双 EventSource。
  private reconnectGen = 0
  private listeners: Map<string, Set<(data: any) => void>> = new Map()

  constructor() {
    // 初始化监听器映射
    this.listeners.set('open', new Set())
    this.listeners.set('snapshot', new Set())
    this.listeners.set('trade', new Set())
    this.listeners.set('market_status', new Set())
    this.listeners.set('ping', new Set())
    this.listeners.set('error', new Set())
  }

  // 连接到市场实时数据流（用户初始化）
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
      ['snapshot', 'trade', 'market_status', 'ping'].includes(d.type as string)
    )
  }

  // 设置事件处理器
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

    // 处理命名事件（后端使用 event: trade / market_status / snapshot / ping）
    this.eventSource.addEventListener('snapshot', handleNamedEvent('snapshot'))
    this.eventSource.addEventListener('trade', handleNamedEvent('trade'))
    this.eventSource.addEventListener('market_status', handleNamedEvent('market_status'))
    this.eventSource.addEventListener('ping', handleNamedEvent('ping'))

    // 处理消息事件
    this.eventSource.onmessage = (event) => {
      const data = parsePayload(event.data)
      if (!data) return
      this.handleEvent(data)
    }

    // 处理错误事件
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

      if (this.marketId === null || this.reconnectAttempts >= this.maxReconnectAttempts) {
        console.error('Max reconnection attempts reached')
        this.emit('error', { type: 'max_reconnect_attempts', message: 'Max reconnection attempts reached' })
        return
      }

      this.reconnectAttempts++
      const targetMarketId = this.marketId
      const targetGen = this.reconnectGen
      // jitter ±30%：防止 N 个客户端同时断开后同步重连打 backend（thundering herd）
      const base = this.reconnectDelay * this.reconnectAttempts
      const delay = base * (0.7 + Math.random() * 0.6)
      setTimeout(() => {
        // 期间用户 disconnect / 切换市场 / 已重连 → 放弃这次定时重连
        if (this.reconnectGen !== targetGen) return
        if (this.marketId !== targetMarketId) return
        if (import.meta.env.DEV) console.log(`Reconnecting... attempt ${this.reconnectAttempts}`)
        this._openConnection(targetMarketId)
      }, delay)
    }

    // 处理打开事件
    this.eventSource.onopen = () => {
      if (import.meta.env.DEV) console.log('EventSource connection opened')
      this.reconnectAttempts = 0
      this.emit('open', { market_id: this.marketId })
    }
  }

  // 处理事件
  private handleEvent(event: MarketEvent) {
    const { type } = event
    
    // 触发对应类型的监听器
    this.emit(type, event)
    
    // 如果是ping事件，可以忽略或处理心跳
    if (type === 'ping') {
      // 心跳包，可以更新连接状态
      if (import.meta.env.DEV) console.debug('Received ping event')
    }
  }

  // 处理错误
  private handleError(error: any) {
    this.emit('error', { type: 'connection_error', error })
  }

  // 添加事件监听器
  on(eventType: string, callback: (data: any) => void) {
    if (!this.listeners.has(eventType)) {
      this.listeners.set(eventType, new Set())
    }
    this.listeners.get(eventType)!.add(callback)
  }

  // 移除事件监听器
  off(eventType: string, callback: (data: any) => void) {
    if (this.listeners.has(eventType)) {
      this.listeners.get(eventType)!.delete(callback)
    }
  }

  // 触发事件
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

  // 断开连接（用户主动）
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

  // 获取当前连接状态
  get isConnected(): boolean {
    return this.eventSource !== null && this.eventSource.readyState === EventSource.OPEN
  }

  // 获取当前市场ID
  get currentMarketId(): number | null {
    return this.marketId
  }

  // 获取重连尝试次数
  get reconnectCount(): number {
    return this.reconnectAttempts
  }
}
