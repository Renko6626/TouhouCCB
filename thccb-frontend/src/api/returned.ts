import api, { apiBaseUrl } from './index'

export type ReturnedStatus = 'open' | 'closed' | 'settled' | 'cancelled'
export interface ReturnedRound {
  round_key: string
  game_key: string
  round_number: number
  left_label: string
  right_label: string
  team_name: string
  status: ReturnedStatus
  stake: string
  bands: Array<{ max_distance: number; payout: string }>
  target_position: number | null
  created_at: string
  closed_at: string | null
  finished_at: string | null
}

export interface ReturnedPrediction {
  round_key: string
  status: ReturnedStatus
  position: number | null
  stake: string
  distance: number | null
  payout: string | null
  refund: string
  net_profit: string | null
  submitted_at: string | null
}

const prefix = '/api/v1/returned'
export const returnedApi = {
  current: () => api.get<ReturnedRound | null>(`${prefix}/current`),
  mine: (key: string) => api.getForCurrentUser<ReturnedPrediction>(`${prefix}/rounds/${encodeURIComponent(key)}/me`),
  submit: (key: string, position: number) => api.putForCurrentUser<ReturnedPrediction>(
    `${prefix}/rounds/${encodeURIComponent(key)}/prediction`, { position }),
  subscribe: (changed: () => void, connection: (connected: boolean) => void) => {
    const stream = new EventSource(`${apiBaseUrl.replace(/\/$/, '')}${prefix}/stream`)
    stream.onopen = () => connection(true)
    stream.onerror = () => connection(false)
    stream.addEventListener('sync', changed)
    stream.addEventListener('round_changed', changed)
    return () => stream.close()
  },
}

export interface ReturnedConfig {
  enabled: boolean
  returned_url: string
  secret_configured: boolean
  active_round_key: string | null
}

export const returnedAdminApi = {
  getConfig: () => api.get<ReturnedConfig>('/api/v1/admin/returned/config'),
  saveConfig: (value: { enabled: boolean; returned_url: string; secret: string }) =>
    api.put<ReturnedConfig>('/api/v1/admin/returned/config', value),
  cancelRound: (key: string) => api.post(`/api/v1/admin/returned/rounds/${encodeURIComponent(key)}/cancel`),
}
