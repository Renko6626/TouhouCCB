import type { BatchAdjustFilter } from '@/api/admin'

export interface UserFilterState {
  userIdMin: string
  userIdMax: string
  cashMin: string
  cashMax: string
  debtMin: string
  debtMax: string
  isActive: 'all' | 'active' | 'inactive'
  includeSuperuser: boolean
}

export function emptyUserFilter(): UserFilterState {
  return {
    userIdMin: '', userIdMax: '', cashMin: '', cashMax: '', debtMin: '', debtMax: '',
    isActive: 'all', includeSuperuser: false,
  }
}

const toOptStr = (s: string): string | undefined => (s.trim() === '' ? undefined : s.trim())
const toOptInt = (s: string): number | undefined => {
  const t = s.trim()
  if (t === '') return undefined
  const n = parseInt(t, 10)
  return Number.isFinite(n) ? n : undefined
}

export function toApiFilter(f: UserFilterState): BatchAdjustFilter {
  return {
    user_id_min: toOptInt(f.userIdMin),
    user_id_max: toOptInt(f.userIdMax),
    cash_min: toOptStr(f.cashMin),
    cash_max: toOptStr(f.cashMax),
    debt_min: toOptStr(f.debtMin),
    debt_max: toOptStr(f.debtMax),
    is_active: f.isActive === 'all' ? null : f.isActive === 'active',
    include_superuser: f.includeSuperuser,
  }
}
