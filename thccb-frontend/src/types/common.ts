// 通用类型定义

// 错误响应类型
export interface ErrorResponse {
  detail: string
}

// 通用响应类型
export interface ApiResponse<T = any> {
  data?: T
  error?: string
  message?: string
}

export interface MessageResponse {
  message: string
}