import api from './index'

// ── PvE 机器人管理（backend/app/api/v1/admin_pve.py）──

export type PveBotStatus = 'active' | 'paused' | 'dead'

export interface PveBotItem {
  profile_id: number
  user_id: number
  username: string
  template: string
  status: PveBotStatus
  params: Record<string, unknown>
  market_scope: number[] | null
  cash: number
  holdings_value: number
  total_value: number
  today_turnover: number
  scheduled: boolean
  next_action_at: string | null
  last_trade_at: string | null
  created_at: string
}

export interface PveOverview {
  enabled: boolean
  counts: { active: number; paused: number; dead: number }
  engine: {
    scheduled_bots: number
    orders_last_min: number
    last_tick: Record<string, unknown>
  }
  templates: string[]
  active_presets: string[]
}

export interface PveLogEntry {
  ts: string
  event: string
  msg: string
}

export interface PveGenerateRequest {
  items: { template: string; count: number }[]
  naming_style: 'npc' | 'lowkey'
  initial_cash: string
  market_scope?: number[] | null
}

export interface PveGeneratedBot {
  profile_id: number
  user_id: number
  username: string
  template: string
}

export interface PvePatchRequest {
  status?: 'active' | 'paused'
  template?: string
  params?: Record<string, unknown>
  market_scope?: number[] | null
}

export interface PveConfigEntry {
  value: string
  value_type: 'bool' | 'int' | 'decimal'
  is_default: boolean
}

const P = '/api/v1/admin/pve'

export const pveApi = {
  overview: () => api.get<PveOverview>(`${P}/overview`),
  listBots: () => api.get<PveBotItem[]>(`${P}/bots`),
  generate: (payload: PveGenerateRequest) =>
    api.post<{ created: PveGeneratedBot[] }>(`${P}/bots/generate`, payload),
  patchBot: (profileId: number, payload: PvePatchRequest) =>
    api.patch<{ ok: boolean; changes: string[] }>(`${P}/bots/${profileId}`, payload),
  fund: (profileId: number, amount: string, reason?: string) =>
    api.post<{ ok: boolean; new_cash: number; status: PveBotStatus }>(
      `${P}/bots/${profileId}/fund`, { amount, reason },
    ),
  log: (profileId: number) =>
    api.get<{ profile_id: number; log: PveLogEntry[] }>(`${P}/bots/${profileId}/log`),
  getConfig: () => api.get<Record<string, PveConfigEntry>>(`${P}/config`),
  putConfig: (payload: Record<string, string>) =>
    api.put<{ ok: boolean; applied: Record<string, string> }>(`${P}/config`, payload),
}
