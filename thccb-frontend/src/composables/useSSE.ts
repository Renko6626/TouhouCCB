import { ref, onBeforeUnmount } from 'vue'
import { MarketStream } from '@/api/stream'

/**
 * SSE实时流连接组合式函数
 * 基于现有的MarketStream类，提供更简洁的Vue组合式API
 */
export function useSSE() {
  const stream = ref<MarketStream | null>(null)
  const isConnected = ref(false)
  const reconnectCount = ref(0)

  const handleOpen = () => {
    isConnected.value = true
    reconnectCount.value = 0
  }

  const handleError = (data: any) => {
    isConnected.value = false
    if (data?.type === 'max_reconnect_attempts') {
      console.error('SSE连接重试失败')
    }
  }

  const ensureStream = () => {
    if (!stream.value) {
      stream.value = new MarketStream()
      stream.value.on('open', handleOpen)
      stream.value.on('error', handleError)
    }
    return stream.value
  }

  // 连接到指定市场
  const connect = (marketId: number) => {
    const s = ensureStream()
    s.connect(marketId)
    isConnected.value = s.isConnected
    reconnectCount.value = s.reconnectCount
  }

  // 断开连接
  const disconnect = () => {
    if (stream.value) {
      stream.value.disconnect()
      isConnected.value = false
    }
  }

  // 添加事件监听器
  const on = (eventType: string, callback: (data: any) => void) => {
    ensureStream().on(eventType, callback)
  }

  // 移除事件监听器
  const off = (eventType: string, callback: (data: any) => void) => {
    if (stream.value) {
      stream.value.off(eventType, callback)
    }
  }

  onBeforeUnmount(() => {
    if (stream.value) {
      stream.value.off('open', handleOpen)
      stream.value.off('error', handleError)
      stream.value.disconnect()
    }
  })

  return {
    stream,
    isConnected,
    reconnectCount,
    connect,
    disconnect,
    on,
    off,
  }
}

// 类型导出
export type UseSSEReturn = ReturnType<typeof useSSE>