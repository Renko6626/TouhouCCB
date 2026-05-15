import api from './index'

export interface DanmukuExchangeRequest {
  qq_user_id: string
  room_id: string
  yuan: number | string
  huo: number | string
}

export interface DanmukuExchangeResponse {
  id: number
  code_string: string
  yuan: number
  huo: number
  amount: number
  cash_after: number
  timestamp: string
}

export interface DanmukuExchangeHistoryItem {
  id: number
  qq_user_id: string
  room_id: string
  yuan: number
  huo: number
  amount: number
  code_string: string
  timestamp: string
}

export const danmukuApi = {
  exchange: (data: DanmukuExchangeRequest) =>
    api.post<DanmukuExchangeResponse>('/api/v1/danmuku/exchange', data),
  myHistory: () =>
    api.get<DanmukuExchangeHistoryItem[]>('/api/v1/danmuku/my'),
}
