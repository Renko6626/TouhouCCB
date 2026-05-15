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
  // 已登录但尚未勾选免责声明的用户：前端 TosModal 兜底（后端不卡接口）。
  // 显式判 === null：localStorage 中的老 user 对象可能没有 tos_accepted_at 字段
  // （undefined），此时不弹窗，等 /auth/me 刷新后再做判定，避免初始化时一闪而过。
  const needsTos = computed(
    () => isAuthenticated.value && user.value !== null && user.value.tos_accepted_at === null,
  )

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

  const acceptTos = async () => {
    const res = await authApi.acceptTos()
    if (user.value) {
      user.value = { ...user.value, tos_accepted_at: res.tos_accepted_at }
      localStorage.setItem('user', JSON.stringify(user.value))
    }
  }

  // 用户改昵称后同步本地 user.username（避免 AppHeader / Portfolio 等地方还显示旧名）
  const patchUsername = (newName: string) => {
    if (user.value) {
      user.value = { ...user.value, username: newName }
      localStorage.setItem('user', JSON.stringify(user.value))
    }
  }

  return {
    user,
    accessToken,
    refreshToken,
    isAuthenticated,
    isAdmin,
    needsTos,
    fetchCurrentUser,
    loginWithCallback,
    logout,
    checkAuth,
    acceptTos,
    patchUsername,
  }
})
