import api from './index'
import type {
  User,
  LoginRequest,
  LoginResponse,
  RegisterRequest,
  ActivateRequest,
  ActivateResponse,
  ActivationCode,
  MessageResponse
} from '@/types/api'

export const authApi = {
  // 用户登录
  async login(credentials: LoginRequest): Promise<LoginResponse> {
    // fastapi-users 的登录端点需要 form-urlencoded 格式
    const formData = new URLSearchParams()
    formData.append('username', credentials.username)
    formData.append('password', credentials.password)
    
    return api.post<LoginResponse>('/api/v1/auth/jwt/login', formData, {
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
      },
    })
  },

  // 用户注册
  async register(userData: RegisterRequest): Promise<User> {
    return api.post<User>('/api/v1/auth/register', userData)
  },

  // 获取当前用户信息
  async getCurrentUser(): Promise<User> {
    return api.get<User>('/api/v1/auth/me')
  },

  // 使用激活码激活账号
  async activate(code: string): Promise<ActivateResponse> {
    const request: ActivateRequest = { code }
    return api.post<ActivateResponse>('/api/v1/auth/activate', request)
  },

  // 管理员：批量生成激活码
  async generateActivationCodes(count: number, length: number = 16): Promise<{ count: number; codes: string[] }> {
    return api.post<{ count: number; codes: string[] }>('/api/v1/auth/admin/activation-codes/batch', {
      count,
      length
    })
  },

  // 管理员：查看激活码列表
  async getActivationCodes(used?: boolean, limit: number = 200): Promise<ActivationCode[]> {
    const params: any = { limit }
    if (used !== undefined) {
      params.used = used
    }
    
    return api.get<ActivationCode[]>('/api/v1/auth/admin/activation-codes', { params })
  },

  // 管理员：作废激活码
  async deleteActivationCode(codeId: number): Promise<MessageResponse | void> {
    return api.delete<MessageResponse | void>(`/api/v1/auth/admin/activation-codes/${codeId}`)
  }
}

export default authApi
