import type { FxChartPoint } from '@/types/fx'

export const FX_DAY_MS = 24 * 60 * 60 * 1000
export const FX_OVERVIEW_BUCKET_MS = 15 * 60 * 1000

/** A candle close is only known at its bucket end; never use a crossing candle. */
export function fxOverviewTrend(points: FxChartPoint[], price: string | undefined, now: number) {
  const cutoff = now - FX_DAY_MS
  const sorted = points.map(point => ({ t: Date.parse(point.t), c: point.c }))
    .filter(point => Number.isFinite(point.t) && Number.isFinite(point.c) && point.c > 0 && point.t <= now)
    .sort((a, b) => a.t - b.t)
  // Restrict approximation to the preceding 15m, rather than claiming stale prices are 24h old.
  const candidates = sorted.filter(point => point.t + FX_OVERVIEW_BUCKET_MS <= cutoff
    && point.t + FX_OVERVIEW_BUCKET_MS > cutoff - FX_OVERVIEW_BUCKET_MS)
  const baseline = candidates[candidates.length - 1]
  const current = price === undefined ? NaN : Number(price)
  const rawChange = baseline && Number.isFinite(current) && current > 0 ? (current / baseline.c - 1) * 100 : null
  const change = rawChange !== null && Number.isFinite(rawChange) ? rawChange : null
  const visible = sorted.filter(point => point.t >= cutoff)
  const values = visible.map(point => point.c)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min
  const path = visible.length < 2 ? '' : visible.map(point => {
    const x = ((point.t - cutoff) / FX_DAY_MS) * 240
    const y = span ? 42 - ((point.c - min) / span) * 36 : 24
    return `${x.toFixed(2)},${y.toFixed(2)}`
  }).join(' ')
  return { change, path }
}
