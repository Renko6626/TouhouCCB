import axios from 'axios'
import type { AxiosInstance, AxiosRequestConfig } from 'axios'
import { useAuthStore } from '@/stores/auth'

// 创建axios实例
const instance: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8004',
  timeout: 10000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// 请求拦截器 - 添加认证令牌
instance.interceptors.request.use((config) => {
  const authStore = useAuthStore()
  const token = authStore.accessToken
  
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  
  return config
})

// 响应拦截器 - 统一错误处理
instance.interceptors.response.use(
  (response) => {
    // 直接返回响应数据
    return response.data
  },
  (error) => {
    const authStore = useAuthStore()
    
    if (error.response?.status === 401) {
      // 认证失败，清除登录状态
      authStore.logout()
      // 可以跳转到登录页
      window.location.href = '/auth/login'
    }
    
    // 统一错误处理逻辑
    const message = error.response?.data?.detail || error.message || '请求失败'
    console.error('API Error:', error)
    
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
