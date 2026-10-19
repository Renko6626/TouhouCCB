import api from './index'

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
