import { describe, expect, it } from 'vitest'
import { FX_DAY_MS, fxOverviewTrend } from '../fxOverview'
import type { FxChartPoint } from '@/types/fx'
const now = Date.parse('2026-09-30T12:07:00Z')
const cutoff = now - FX_DAY_MS
const point = (t: string, c: number): FxChartPoint => ({ t, c, o: c, h: c, l: c, v: 0 })
describe('FX overview 24h baseline', () => {
  it('uses a completed preceding bucket and ignores the boundary-crossing candle', () => {
    expect(fxOverviewTrend([point('2026-09-29T11:45:00Z', 100), point('2026-09-29T12:00:00Z', 200)], '110', now).change).toBeCloseTo(10)
  })
  it('refuses missing, stale or crossing-only baselines', () => {
    expect(fxOverviewTrend([], '110', now).change).toBeNull()
    expect(fxOverviewTrend([point('2026-09-29T11:30:00Z', 100)], '110', now).change).toBeNull()
    expect(fxOverviewTrend([point('2026-09-29T12:00:00Z', 100)], '110', now).change).toBeNull()
  })
  it('keeps invalid prices unavailable and flat sparklines finite', () => {
    const points = [point(new Date(cutoff + 1000).toISOString(), 10), point(new Date(now).toISOString(), 10)]
    expect(fxOverviewTrend(points, undefined, now)).toEqual({ change: null, path: '0.00,24.00 240.00,24.00' })
  })
})
