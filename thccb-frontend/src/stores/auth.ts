import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { User } from '@/types/api'
import { authApi } from '@/api/auth'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)
  const accessToken = ref<string | null>(localStorage.getItem('access_token'))
  const refreshToken = ref<string | null>(localStorage.getItem('refresh_token'))

  const isAuthenticated = computed(() => !!accessToken.value && !!user.value)
  const isAdmin = computed(() => user.value?.is_superuser ?? false)

  // 初始化时从 localStorage 恢复用户信息
  const stored = localStorage.getItem('user')
  if (stored) {
    try {
      user.value = JSON.parse(stored)
    } catch (e) {
      console.warn('[Auth] localStorage 中的用户数据解析失败，已清除:', e)
      localStorage.removeItem('user')
    }
  }

  let isFetchingUser = false

  const fetchCurrentUser = async () => {
    if (!accessToken.value || isFetchingUser) return null
    isFetchingUser = true
    try {
      user.value = await authApi.getCurrentUser()
      localStorage.setItem('user', JSON.stringify(user.value))
      return user.value
    } catch (err: any) {
      if (err?.status === 401) logout()
      return null
    } finally {
      isFetchingUser = false
    }
  }

  // Casdoor 回调后：用 code 换 token，再拉取用户信息
  const loginWithCallback = async (code: string, state: string) => {
    const { access_token, refresh_token } = await authApi.oauthCallback(code, state)
    accessToken.value = access_token
    refreshToken.value = refresh_token
    localStorage.setItem('access_token', access_token)
    localStorage.setItem('refresh_token', refresh_token)
    await fetchCurrentUser()
  }

  let isLoggingOut = false

  const logout = () => {
    if (isLoggingOut) return
    isLoggingOut = true
    user.value = null
    accessToken.value = null
    refreshToken.value = null
    localStorage.removeItem('access_token')
    localStorage.removeItem('refresh_token')
    localStorage.removeItem('user')
    window.location.href = '/auth/login'
  }

  const checkAuth = async () => {
    if (!accessToken.value) return false
    const u = await fetchCurrentUser()
    return !!u
  }

  return {
    user,
    accessToken,
    refreshToken,
    isAuthenticated,
    isAdmin,
    fetchCurrentUser,
    loginWithCallback,
    logout,
    checkAuth,
  }
})
