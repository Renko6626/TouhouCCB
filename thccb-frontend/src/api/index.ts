import axios from 'axios'
import type { AxiosInstance, AxiosRequestConfig, InternalAxiosRequestConfig } from 'axios'
import { useAuthStore } from '@/stores/auth'

interface UserBoundConfig extends AxiosRequestConfig { authContextGuard?: () => boolean }
type UserBoundInternalConfig = InternalAxiosRequestConfig & { _retry?: boolean; authContextGuard?: () => boolean }
function changedAuthContext() {
  return { code: 'AUTH_CONTEXT_CHANGED', message: '登录账号已变化，原请求保留待确认，请切回原账号核对。' }
}
function assertAuthContext(config: { authContextGuard?: () => boolean } | undefined) {
  if (config?.authContextGuard && !config.authContextGuard()) throw changedAuthContext()
}

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
let activeRefreshContext: (() => boolean) | null = null
let pendingRequests: Array<{ resolve: (token: string) => void; reject: (err: any) => void }> = []

function onRefreshed(token: string) {
  pendingRequests.forEach((p) => p.resolve(token))
  pendingRequests = []
}

function onRefreshFailed(err: any) {
  pendingRequests.forEach((p) => p.reject(err))
  pendingRequests = []
}

// 请求拦截器 - 添加认证令牌
instance.interceptors.request.use(async (config) => {
  assertAuthContext(config as UserBoundInternalConfig)
  const authStore = useAuthStore()
  const token = authStore.accessToken

  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }

  // Anti-bot L2: /market/{buy,sell,quote} 加 X-Client-Token + X-Client-TS
  // activity_mode=false (默认) 时后端不验；始终发，避免分支
  if (config.url?.match(/\/api\/v1\/market\/(buy|sell|quote)/)) {
    // dynamic import 避免循环依赖 (api → store → api)
    const { useAuthStore: useAuthStoreDynamic } = await import('@/stores/auth')
    const authStoreDynamic = useAuthStoreDynamic()
    const uid = authStoreDynamic.user?.id
    if (uid) {
      const { generateClientToken } = await import('@/utils/clientToken')
      const { token: clientToken, ts } = await generateClientToken(uid)
      if (clientToken) {
        config.headers['X-Client-Token'] = clientToken
        config.headers['X-Client-TS'] = String(ts)
      }
    }
  }

  return config
})

// 响应拦截器 - 统一错误处理 + 自动 refresh
instance.interceptors.response.use(
  (response) => {
    assertAuthContext(response.config as UserBoundInternalConfig)
    return response.data
  },
  async (error) => {
    if (error.code === 'AUTH_CONTEXT_CHANGED') return Promise.reject(error)
    const originalRequest = error.config as UserBoundInternalConfig
    assertAuthContext(originalRequest)

    // 403 + detail USER_BANNED → 触发全局封号弹窗 (BannedDialog 监听这个事件)
    // 后端 core/users.py + auth.py 用此 marker 区分"被封"vs"无效凭证"
    if (
      error.response?.status === 403 &&
      error.response?.data?.detail === 'USER_BANNED'
    ) {
      window.dispatchEvent(new CustomEvent('user-banned'))
      return Promise.reject({
        message: '账号已被封禁',
        status: 403,
        data: error.response?.data,
      })
    }

    // 403 + detail MARKET_TITLE_REQUIRED → 触发全局事件供 toast 监听
    if (
      error.response?.status === 403 &&
      error.response?.data?.detail === 'MARKET_TITLE_REQUIRED'
    ) {
      window.dispatchEvent(new CustomEvent('market-title-required', {
        detail: error.response.data,
      }))
      return Promise.reject({
        message: '此市场需要特定称号',
        status: 403,
        data: error.response?.data,
      })
    }

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
        if (originalRequest.authContextGuard && activeRefreshContext && !activeRefreshContext())
          return Promise.reject(changedAuthContext())
        // 已有一个 refresh 在进行，排队等新 token
        return new Promise((resolve, reject) => {
          pendingRequests.push({
            resolve: (newToken: string) => {
              try { assertAuthContext(originalRequest) }
              catch (e) { reject(e); return }
              originalRequest.headers.Authorization = `Bearer ${newToken}`
              originalRequest._retry = true
              resolve(instance(originalRequest))
            },
            reject,
          })
        })
      }

      isRefreshing = true
      originalRequest._retry = true
      const refreshUserId = authStore.user?.id
      const refreshToken = authStore.refreshToken
      const sameRefreshContext = () => authStore.user?.id === refreshUserId && authStore.refreshToken === refreshToken
      activeRefreshContext = sameRefreshContext

      try {
        const { data } = await axios.post(
          `${instance.defaults.baseURL}/api/v1/auth/refresh`,
          { refresh_token: authStore.refreshToken },
        )
        // An unbound request can also have started this shared refresh. Never
        // install its token into a later login or supply it to that login's queue.
        if (!sameRefreshContext()) throw changedAuthContext()
        assertAuthContext(originalRequest)
        const newToken = data.access_token
        authStore.accessToken = newToken
        localStorage.setItem('access_token', newToken)

        originalRequest.headers.Authorization = `Bearer ${newToken}`
        onRefreshed(newToken)
        return instance(originalRequest)
      } catch (refreshError) {
        onRefreshFailed(refreshError)
        if (!sameRefreshContext() || (originalRequest.authContextGuard && !originalRequest.authContextGuard()))
          return Promise.reject(changedAuthContext())
        authStore.logout()
        return Promise.reject(error)
      } finally {
        isRefreshing = false
        activeRefreshContext = null
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
  /** Bound economic requests cannot be refreshed or replayed under a new login. */
  postForCurrentUser: <T>(url: string, data: unknown, config?: AxiosRequestConfig): Promise<T> => {
    const auth = useAuthStore(), userId = auth.user?.id, refreshToken = auth.refreshToken
    if (userId == null) return Promise.reject(changedAuthContext())
    const bound: UserBoundConfig = { ...config,
      authContextGuard: () => auth.user?.id === userId && auth.refreshToken === refreshToken }
    return instance.post(url, data, bound).then(res => res as T)
  },
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
