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
