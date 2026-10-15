import api from './index'
export interface Field {
  type?: string
  title?: string
  default?: unknown
  minimum?: number
  maximum?: number
  readOnly?: boolean
  unit?: string
  anyOf?: Field[]
}
export interface Bot {
  profile_id: number
  username: string
  pair_id: number
  template: string
  status: string
  cash: string
  wallet: string
  mtm: string
  liquidation_cash: string
  inventory_baseline_foreign: string
  params: Record<string, unknown>
  strategy_state: Record<string, unknown>
}
export interface Config {
  enabled: boolean
  pair_configs: Record<string, Record<string, unknown>>
}
export interface Overview {
  templates: {
    template: string
    description: string
    editable_when: string
    params_schema: { properties: Record<string, Field> }
  }[]
  pair_config_schema: { properties: Record<string, Field> }
  preset: {
    items: { template: string; count: number }[]
    initial_cash: string
    initial_foreign: string
  }
  pairs: { id: number; code: string; price: string; status: string; archived: boolean }[]
  engine: Record<string, unknown>
}
export interface Patch {
  status?: 'active' | 'paused'
  template?: string
  params?: Record<string, unknown>
}
const P = '/api/v1/admin/pve/fx'
export const pveFxApi = {
  overview: () => api.get<Overview>(`${P}/overview`),
  config: () => api.get<Config>(`${P}/config`),
  saveConfig: (v: Config) => api.put(`${P}/config`, v),
  bots: () => api.get<Bot[]>(`${P}/bots`),
  generate: (v: unknown) => api.post(`${P}/bots/generate`, v),
  patch: (id: number, v: Patch) => api.patch(`${P}/bots/${id}`, v),
  batch: (ids: number[], patch: Patch) => api.post(`${P}/bots/batch`, { profile_ids: ids, patch }),
  fund: (id: number, amount: string) => api.post(`${P}/bots/${id}/fund`, { amount }),
  liquidate: (id: number, min_out: string) =>
    api.post<{ bot: Bot }>(`${P}/bots/${id}/liquidate`, { min_out }),
  retire: (id: number) => api.delete(`${P}/bots/${id}`),
  log: (id: number) => api.get<{ log: unknown[] }>(`${P}/bots/${id}/log`),
}
