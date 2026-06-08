import api from './index'
import type { User } from '@/types/api'

export const authApi = {
  async oauthCallback(code: string, state: string): Promise<{ access_token: string; refresh_token: string }> {
    return api.post('/api/v1/auth/callback', {
      code,
      state,
      redirect_uri: `${window.location.origin}/auth/callback`,
    })
  },

  async refreshToken(refresh_token: string): Promise<{ access_token: string }> {
    return api.post('/api/v1/auth/refresh', { refresh_token })
  },

  // 仅本地开发：mock 登录（后端在生产环境返回 404）
  async devLogin(username: string): Promise<{ access_token: string; refresh_token: string }> {
    return api.post('/api/v1/auth/dev-login', { username })
  },

  async getCurrentUser(): Promise<User> {
    return api.get<User>('/api/v1/auth/me')
  },

  async acceptTos(): Promise<{ tos_accepted_at: string }> {
    return api.post('/api/v1/auth/accept-tos', {})
  },
}

export default authApi
