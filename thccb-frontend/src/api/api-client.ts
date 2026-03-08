import axios, { type AxiosInstance, type AxiosRequestConfig, type AxiosResponse } from 'axios'
import { useAuthStore } from '@/stores/auth'

// 创建自定义的API客户端类
class ApiClient {
  private instance: AxiosInstance

  constructor() {
    this.instance = axios.create({
      baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8004',
      timeout: 10000,
      headers: {
        'Content-Type': 'application/json',
      },
    })

    this.setupInterceptors()
  }

  private setupInterceptors() {
    // 请求拦截器
    this.instance.interceptors.request.use((config) => {
      const authStore = useAuthStore()
      const token = authStore.accessToken
      
      if (token) {
        config.headers.Authorization = `Bearer ${token}`
      }
      
      return config
    })

    // 响应拦截器
    this.instance.interceptors.response.use(
      (response: AxiosResponse) => response.data,
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
  }

  // 泛型请求方法
  async request<T = any>(config: AxiosRequestConfig): Promise<T> {
    return this.instance.request<T>(config)
  }

  async get<T = any>(url: string, config?: AxiosRequestConfig): Promise<T> {
    return this.instance.get<T>(url, config)
  }

  async post<T = any>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> {
    return this.instance.post<T>(url, data, config)
  }

  async put<T = any>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> {
    return this.instance.put<T>(url, data, config)
  }

  async delete<T = any>(url: string, config?: AxiosRequestConfig): Promise<T> {
    return this.instance.delete<T>(url, config)
  }

  async patch<T = any>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> {
    return this.instance.patch<T>(url, data, config)
  }
}

// 导出单例实例
export const apiClient = new ApiClient()

// 为了向后兼容，也导出默认实例
export default apiClient