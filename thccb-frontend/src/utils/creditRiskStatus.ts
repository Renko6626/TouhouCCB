export type AuthoritativeRiskStatus = 'healthy' | 'warning' | 'danger' | 'blocked' | 'unknown'
export type CreditRiskStatus = AuthoritativeRiskStatus | 'protected' | 'none'
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
  protected?: boolean
  legacy?: boolean
  noRisk?: boolean
}): CreditRiskStatus {
  if (options.blocked || options.authoritativeStatus === 'blocked') return 'blocked'
  if (!options.legacy) {
    if (!options.authoritativeStatus || options.authoritativeStatus === 'unknown') return 'unknown'
    return options.noRisk ? 'none' : options.authoritativeStatus
  }
  if (options.protected) return 'protected'
  if (options.noRisk) return 'none'
  const initial = riskThreshold(options.initial ?? null)
  const maintenance = riskThreshold(options.maintenance ?? null)
  if (options.ratio == null || !Number.isFinite(options.ratio) || initial == null || maintenance == null) return 'unknown'
  if (options.ratio < maintenance) return 'danger'
  if (options.ratio < initial) return 'warning'
  return 'healthy'
}
