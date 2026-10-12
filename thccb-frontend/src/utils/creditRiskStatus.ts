import type { AccountPreview } from './tradingRisk'
import type { UserSummary } from '@/types/user'
export type AuthoritativeRiskStatus = 'healthy' | 'warning' | 'danger' | 'blocked' | 'unknown'
export type CreditRiskStatus = AuthoritativeRiskStatus | 'none'
type Threshold = string | number | null

export function riskThreshold(value: Threshold): number | null {
  if (value == null) return null
  const result = Number(value)
  return Number.isFinite(result) && result >= 0 ? result : null
}

export function resolveCreditRiskStatus(options: {
  ratio: number | null
  initial?: Threshold
  maintenance?: Threshold
  authoritativeStatus?: AuthoritativeRiskStatus | null
  blocked?: boolean
  noRisk?: boolean
}): CreditRiskStatus {
  if (options.blocked || options.authoritativeStatus === 'blocked') return 'blocked'
  if (!options.authoritativeStatus || options.authoritativeStatus === 'unknown') return 'unknown'
  return options.noRisk ? 'none' : options.authoritativeStatus
}

/** Trading routes use local admission; home callers retain their authoritative summary. */
export function resolveMarginBannerState(options: {
  trading: boolean; authenticated: boolean; ready: boolean
  preview: Pick<AccountPreview, 'risk_status' | 'initial_condition' | 'maintenance_condition'> | null
  summary: Pick<UserSummary, 'risk_status' | 'blocked_reason'> | null
}): { status: AuthoritativeRiskStatus; visible: boolean; blockedReason: string | null } {
  let status: AuthoritativeRiskStatus = 'healthy'
  if (options.trading) {
    const p = options.preview
    status = !options.ready || !p || p.risk_status === 'unknown' ? 'blocked'
      : p.maintenance_condition === 'rejected' ? 'danger'
        : p.initial_condition === 'rejected' ? 'warning' : 'healthy'
  } else if (options.summary) status = options.summary.risk_status ?? 'blocked'
  return { status, visible: options.authenticated && (options.trading || !!options.summary) && status !== 'healthy',
    blockedReason: !options.trading ? options.summary?.blocked_reason ?? null : null }
}
