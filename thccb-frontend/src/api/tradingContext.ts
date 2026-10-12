import api from './index'
import type { GroupKey, TradingContext, TradingPrices, TradingVersionCheck } from '@/types/tradingContext'
export const groupKey = (group: GroupKey) => `${group.product}:${group.id}`
export function groupQuery(group?: GroupKey | null) {
  const params = new URLSearchParams()
  if (group) { params.set('current_product', group.product); params.set('current_id', String(group.id)) }
  return params
}
export const tradingContextApi = {
  getContext: (group?: GroupKey | null, signal?: AbortSignal) => api.getForCurrentUser<TradingContext>(`/api/v1/user/trading-context?${groupQuery(group)}`, { signal }),
  getVersions: (group?: GroupKey | null, signal?: AbortSignal) => api.getForCurrentUser<TradingVersionCheck>(`/api/v1/user/trading-versions?${groupQuery(group)}`, { signal }),
  getPrices: (keys: GroupKey[], group?: GroupKey | null, signal?: AbortSignal) => {
    const params = groupQuery(group)
    keys.forEach(key => params.append('key', groupKey(key)))
    return api.getForCurrentUser<TradingPrices>(`/api/v1/user/trading-prices?${params}`, { signal })
  },
}
