import axios from 'axios'
import type { AxiosInstance, AxiosRequestConfig, InternalAxiosRequestConfig } from 'axios'
import { useAuthStore } from '@/stores/auth'

// 创建axios实例
const instance: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8004',
  timeout: 10000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// refresh token 锁：防止多个 401 同时触发多次刷新
let isRefreshing = false
let pendingRequests: Array<(token: string) => void> = []

function onRefreshed(token: string) {
  pendingRequests.forEach((cb) => cb(token))
  pendingRequests = []
}

// 请求拦截器 - 添加认证令牌
instance.interceptors.request.use((config) => {
  const authStore = useAuthStore()
  const token = authStore.accessToken

  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }

  return config
})

// 响应拦截器 - 统一错误处理 + 自动 refresh
instance.interceptors.response.use(
  (response) => {
    return response.data
  },
  async (error) => {
    const originalRequest = error.config as InternalAxiosRequestConfig & { _retry?: boolean }

    // 401 且不是 refresh 请求本身，尝试用 refresh token 续期
    if (
      error.response?.status === 401 &&
      !originalRequest._retry &&
      !originalRequest.url?.includes('/auth/refresh')
    ) {
      const authStore = useAuthStore()

      if (!authStore.refreshToken) {
        authStore.logout()
        return Promise.reject(error)
      }

      if (isRefreshing) {
        // 已有一个 refresh 在进行，排队等新 token
        return new Promise((resolve) => {
          pendingRequests.push((newToken: string) => {
            originalRequest.headers.Authorization = `Bearer ${newToken}`
            originalRequest._retry = true
            resolve(instance(originalRequest))
          })
        })
      }

      isRefreshing = true
      originalRequest._retry = true

      try {
        const { data } = await axios.post(
          `${instance.defaults.baseURL}/api/v1/auth/refresh`,
          { refresh_token: authStore.refreshToken },
        )
        const newToken = data.access_token
        authStore.accessToken = newToken
        localStorage.setItem('access_token', newToken)

        originalRequest.headers.Authorization = `Bearer ${newToken}`
        onRefreshed(newToken)
        return instance(originalRequest)
      } catch {
        authStore.logout()
        return Promise.reject(error)
      } finally {
        isRefreshing = false
      }
    }

    const message = error.response?.data?.detail || error.message || '请求失败'
    console.error('API Error:', message)

    return Promise.reject({
      message,
      status: error.response?.status,
      data: error.response?.data,
    })
  }
)

// 创建包装函数以提供更好的类型支持
const api = {
  get: <T = any>(url: string, config?: AxiosRequestConfig): Promise<T> => 
    instance.get(url, config).then(res => res as T),
  
  post: <T = any>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> => 
    instance.post(url, data, config).then(res => res as T),
  
  put: <T = any>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> => 
    instance.put(url, data, config).then(res => res as T),
  
  delete: <T = any>(url: string, config?: AxiosRequestConfig): Promise<T> => 
    instance.delete(url, config).then(res => res as T),
  
  patch: <T = any>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> => 
    instance.patch(url, data, config).then(res => res as T),
}

export default api
