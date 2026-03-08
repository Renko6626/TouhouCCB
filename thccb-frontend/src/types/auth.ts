// 认证相关类型定义

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
}

export interface RegisterRequest {
  email: string
  password: string
  username: string
  is_active?: boolean
  is_superuser?: boolean
  is_verified?: boolean
}

export interface ActivateRequest {
  code: string
}

export interface ActivationCode {
  id: number
  code: string
  is_used: boolean
  used_by_user_id?: number
  used_at?: string
  created_at: string
}

// 激活响应类型
export interface ActivateResponse {
  message: string
  username: string
}

// 激活码批量创建请求类型
export interface CreateActivationCodesRequest {
  count: number   // 1-500
  length?: number // 8-64，默认16
}
