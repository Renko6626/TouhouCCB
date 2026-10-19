import api, { apiBaseUrl } from './index'

export type WavelengthStatus = 'open' | 'closed' | 'settled' | 'cancelled'
export interface WavelengthRound {
  round_key: string
  game_key: string
  round_number: number
  left_label: string
  right_label: string
  team_name: string
  status: WavelengthStatus
  stake: string
  bands: Array<{ max_distance: number; payout: string }>
  target_position: number | null
  created_at: string
  closed_at: string | null
  finished_at: string | null
}

export interface WavelengthPrediction {
  round_key: string
  status: WavelengthStatus
  position: number | null
  stake: string
  distance: number | null
  payout: string | null
  refund: string
  net_profit: string | null
  submitted_at: string | null
}

const prefix = '/api/v1/wavelength'
export const wavelengthApi = {
  current: () => api.get<WavelengthRound | null>(`${prefix}/current`),
  mine: (key: string) => api.getForCurrentUser<WavelengthPrediction>(`${prefix}/rounds/${encodeURIComponent(key)}/me`),
  submit: (key: string, position: number) => api.putForCurrentUser<WavelengthPrediction>(
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

export interface WavelengthConfig {
  enabled: boolean
  wavelength_url: string
  secret_configured: boolean
  active_round_key: string | null
}

export const wavelengthAdminApi = {
  getConfig: () => api.get<WavelengthConfig>('/api/v1/admin/wavelength/config'),
  saveConfig: (value: { enabled: boolean; wavelength_url: string; secret: string }) =>
    api.put<WavelengthConfig>('/api/v1/admin/wavelength/config', value),
  cancelRound: (key: string) => api.post(`/api/v1/admin/wavelength/rounds/${encodeURIComponent(key)}/cancel`),
}
