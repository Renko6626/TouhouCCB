import type { MarketEvent } from '@/types/api'

export class MarketStream {
  private eventSource: EventSource | null = null
  private marketId: number | null = null
  private reconnectAttempts = 0
  private maxReconnectAttempts = 5
  private reconnectDelay = 1000
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

  // 连接到市场实时数据流
  connect(marketId: number) {
    if (this.eventSource && this.marketId === marketId) {
      console.log(`Already connected to market ${marketId}`)
      return
    }

    // 关闭现有连接
    this.disconnect()

    this.marketId = marketId
    const baseUrl = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8004'
    const normalizedBaseUrl = baseUrl.replace(/\/$/, '')
    const url = `${normalizedBaseUrl}/api/v1/stream/market/${marketId}`
    
    try {
      this.eventSource = new EventSource(url)
      this.setupEventHandlers()
      this.reconnectAttempts = 0
      console.log(`Connected to market stream: ${marketId}`)
    } catch (error) {
      console.error('Failed to connect to market stream:', error)
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
      
      // 尝试重连
      if (this.reconnectAttempts < this.maxReconnectAttempts) {
        setTimeout(() => {
          this.reconnectAttempts++
          console.log(`Reconnecting... attempt ${this.reconnectAttempts}`)
          if (this.marketId) {
            this.connect(this.marketId)
          }
        }, this.reconnectDelay * this.reconnectAttempts)
      } else {
        console.error('Max reconnection attempts reached')
        this.emit('error', { type: 'max_reconnect_attempts', message: 'Max reconnection attempts reached' })
      }
    }

    // 处理打开事件
    this.eventSource.onopen = () => {
      console.log('EventSource connection opened')
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
      console.debug('Received ping event')
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

  // 断开连接
  disconnect() {
    if (this.eventSource) {
      this.eventSource.close()
      this.eventSource = null
      this.marketId = null
      console.log('Disconnected from market stream')
    }
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
