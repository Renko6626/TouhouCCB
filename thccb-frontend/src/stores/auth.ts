import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { User, LoginRequest, LoginResponse, RegisterRequest } from '@/types/api'
import { authApi } from '@/api/auth'

export const useAuthStore = defineStore('auth', () => {
  // 状态
  const user = ref<User | null>(null)
  const accessToken = ref<string | null>(localStorage.getItem('access_token'))
  const refreshToken = ref<string | null>(localStorage.getItem('refresh_token'))
  const isLoading = ref(false)
  const error = ref<string | null>(null)

  // 计算属性
  const isAuthenticated = computed(() => !!accessToken.value && !!user.value)
  const isAdmin = computed(() => user.value?.is_superuser || false)
  const isVerified = computed(() => user.value?.is_verified || false)

  // 从本地存储加载用户信息
  const loadUserFromStorage = () => {
    const userStr = localStorage.getItem('user')
    if (userStr) {
      try {
        user.value = JSON.parse(userStr)
      } catch (e) {
        console.error('Failed to parse user from localStorage', e)
        localStorage.removeItem('user')
      }
    }
  }

  // 初始化时加载用户信息
  loadUserFromStorage()

  // Actions
  const login = async (credentials: LoginRequest) => {
    isLoading.value = true
    error.value = null
    
    try {
      const response: LoginResponse = await authApi.login(credentials)
      
      // 保存token
      accessToken.value = response.access_token
      localStorage.setItem('access_token', response.access_token)
      
      // 获取用户信息
      await fetchCurrentUser()
      
      return { success: true }
    } catch (err: any) {
      error.value = err.message || '登录失败'
      return { success: false, error: error.value }
    } finally {
      isLoading.value = false
    }
  }

  const register = async (userData: RegisterRequest) => {
    isLoading.value = true
    error.value = null
    
    try {
      const newUser = await authApi.register(userData)
      user.value = newUser
      
      // 保存用户信息到本地存储
      localStorage.setItem('user', JSON.stringify(newUser))
      
      return { success: true, user: newUser }
    } catch (err: any) {
      error.value = err.message || '注册失败'
      return { success: false, error: error.value }
    } finally {
      isLoading.value = false
    }
  }

  const fetchCurrentUser = async () => {
    if (!accessToken.value) {
      return null
    }
    
    isLoading.value = true
    error.value = null
    
    try {
      const currentUser = await authApi.getCurrentUser()
      user.value = currentUser
      
      // 保存用户信息到本地存储
      localStorage.setItem('user', JSON.stringify(currentUser))
      
      return currentUser
    } catch (err: any) {
      error.value = err.message || '获取用户信息失败'
      // 如果获取用户信息失败，清除token
      if (err.status === 401) {
        logout()
      }
      return null
    } finally {
      isLoading.value = false
    }
  }

  const logout = () => {
    user.value = null
    accessToken.value = null
    refreshToken.value = null
    
    // 清除本地存储
    localStorage.removeItem('access_token')
    localStorage.removeItem('refresh_token')
    localStorage.removeItem('user')
    
    // 可以跳转到登录页
    window.location.href = '/auth/login'
  }

  const activateAccount = async (code: string) => {
    isLoading.value = true
    error.value = null
    
    try {
      const result = await authApi.activate(code)
      
      // 重新获取用户信息
      await fetchCurrentUser()
      
      return { success: true, message: result.message }
    } catch (err: any) {
      error.value = err.message || '激活失败'
      return { success: false, error: error.value }
    } finally {
      isLoading.value = false
    }
  }

  // 检查token是否有效
  const checkAuth = async () => {
    if (!accessToken.value) {
      return false
    }
    
    try {
      await fetchCurrentUser()
      return true
    } catch {
      return false
    }
  }

  return {
    // 状态
    user,
    accessToken,
    refreshToken,
    isLoading,
    error,
    
    // 计算属性
    isAuthenticated,
    isAdmin,
    isVerified,
    
    // Actions
    login,
    register,
    fetchCurrentUser,
    logout,
    activateAccount,
    checkAuth,
    loadUserFromStorage,
  }
})