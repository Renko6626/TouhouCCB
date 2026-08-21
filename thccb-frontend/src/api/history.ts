// /history/ 不可变段的取数与解码（阶段 4，spec § 7）。
// 段 URL 含起点 epoch，内容永不变化：浏览器 immutable + nginx proxy_cache
// 双层缓存，重复进入图表几乎零请求。进行中的尾巴不走这里——由 SSE
// snapshot.history_tail 携带（useCandleHistory 组装）。
import type { Candle, ChartInterval } from '@/types/api'

export interface EncodedSegment {
  t0: number; step: number; n_buckets: number
  t: number[]; o: number[]; h: number[]; l: number[]; c: number[]
  v: number[]; trades: number[]
}

/** snapshot.history_tail 的形状：outcome_id(str) → interval → 列式尾巴 */
export type HistoryTailMap = Record<string, Record<string, EncodedSegment>>

/** 封存段长（秒），与后端 RING_SPEC.segment 一致 */
export const SEGMENT_SECONDS: Record<ChartInterval, number> = {
  '10s': 600, '1m': 3600, '15m': 86400, '1h': 604800,
}

const PRICE_FIXED = 1e8

const baseUrl = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8004').replace(/\/$/, '')

export function decodeSegment(seg: EncodedSegment): Candle[] {
  const out: Candle[] = []
  for (let i = 0; i < seg.t.length; i++) {
    out.push({
      t: new Date((seg.t0 + seg.t[i]! * seg.step) * 1000).toISOString(),
      o: seg.o[i]! / PRICE_FIXED, h: seg.h[i]! / PRICE_FIXED,
      l: seg.l[i]! / PRICE_FIXED, c: seg.c[i]! / PRICE_FIXED,
      v: seg.v[i]!, n: seg.trades[i]!,
    })
  }
  return out
}

/** 覆盖 [fromSec, 最后封存边界) 的段起点列表（对齐段长；进行中的段不含） */
export function sealedSegmentEpochs(interval: ChartInterval, fromSec: number, nowSec: number): number[] {
  const seg = SEGMENT_SECONDS[interval]
  const boundary = nowSec - (nowSec % seg)          // 最后封存边界
  let cur = fromSec - (fromSec % seg)
  const epochs: number[] = []
  for (; cur < boundary; cur += seg) epochs.push(cur)
  return epochs
}

/** 取一个封存段；404（理论上只有未封存/未落库的竞态窗口）→ null，调用方跳过 */
export async function fetchSegment(
  outcomeId: number, interval: ChartInterval, epoch: number,
): Promise<EncodedSegment | null> {
  const resp = await fetch(`${baseUrl}/history/o/${outcomeId}/${interval}/${epoch}.json`)
  if (resp.status === 404) return null
  if (!resp.ok) throw new Error(`history segment ${epoch} failed: ${resp.status}`)
  return (await resp.json()) as EncodedSegment
}

/** 客户端 fill：缺桶用 prev_close 平推（v=0 n=0），语义对齐后端 chart.py fill=true。
 *  窗口前无数据时用首根的 open 反向回填，让前置空桶显示横线。 */
export function fillCandles(
  candles: Candle[], stepSec: number, fromSec: number, toSecExclusive: number,
): Candle[] {
  const byEpoch = new Map<number, Candle>()
  for (const c of candles) byEpoch.set(Math.floor(new Date(c.t).getTime() / 1000), c)
  const first = candles[0]
  let prevClose: number | null = first ? first.o : null
  if (prevClose === null) return []
  const out: Candle[] = []
  for (let cur = fromSec - (fromSec % stepSec); cur < toSecExclusive; cur += stepSec) {
    const c = byEpoch.get(cur)
    if (c) {
      out.push(c)
      prevClose = c.c
    } else {
      out.push({
        t: new Date(cur * 1000).toISOString(),
        o: prevClose, h: prevClose, l: prevClose, c: prevClose, v: 0, n: 0,
      })
    }
  }
  return out
}
