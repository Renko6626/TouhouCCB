import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface Notification {
  id: number
  type: 'success' | 'error' | 'warning' | 'info'
  title: string
  message: string
  duration?: number // 自动消失时间（毫秒），0表示不自动消失
  timestamp: number
}

export const useNotificationStore = defineStore('notification', () => {
  // 状态
  const notifications = ref<Notification[]>([])
  let nextId = 1

  // Actions
  const addNotification = (notification: Omit<Notification, 'id' | 'timestamp'>) => {
    const newNotification: Notification = {
      ...notification,
      id: nextId++,
      timestamp: Date.now()
    }
    
    notifications.value.push(newNotification)
    
    // 如果设置了自动消失时间，自动移除
    if (newNotification.duration && newNotification.duration > 0) {
      setTimeout(() => {
        removeNotification(newNotification.id)
      }, newNotification.duration)
    }
    
    return newNotification.id
  }

  const success = (title: string, message: string, duration: number = 3000) => {
    return addNotification({
      type: 'success',
      title,
      message,
      duration
    })
  }

  const error = (title: string, message: string, duration: number = 5000) => {
    return addNotification({
      type: 'error',
      title,
      message,
      duration
    })
  }

  const warning = (title: string, message: string, duration: number = 4000) => {
    return addNotification({
      type: 'warning',
      title,
      message,
      duration
    })
  }

  const info = (title: string, message: string, duration: number = 3000) => {
    return addNotification({
      type: 'info',
      title,
      message,
      duration
    })
  }

  const removeNotification = (id: number) => {
    const index = notifications.value.findIndex(n => n.id === id)
    if (index !== -1) {
      notifications.value.splice(index, 1)
    }
  }

  const clearAll = () => {
    notifications.value = []
  }

  const clearByType = (type: Notification['type']) => {
    notifications.value = notifications.value.filter(n => n.type !== type)
  }

  const clearOld = (maxAge: number = 60000) => { // 默认清除1分钟前的通知
    const now = Date.now()
    notifications.value = notifications.value.filter(n => now - n.timestamp < maxAge)
  }

  return {
    // 状态
    notifications,
    
    // Actions
    addNotification,
    success,
    error,
    warning,
    info,
    removeNotification,
    clearAll,
    clearByType,
    clearOld,
  }
})