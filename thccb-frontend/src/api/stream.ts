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

  // 设置事件处理器
  private setupEventHandlers() {
    if (!this.eventSource) return

    // 处理消息事件
    this.eventSource.onmessage = (event) => {
      try {
        const data: MarketEvent = JSON.parse(event.data)
        this.handleEvent(data)
      } catch (error) {
        console.error('Failed to parse event data:', error, event.data)
      }
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
    }
  }

  // 处理事件
  private handleEvent(event: MarketEvent) {
    const { type, data } = event
    
    // 触发对应类型的监听器
    this.emit(type, data)
    
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
