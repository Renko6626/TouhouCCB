// 图表初始数据组装（阶段 4，spec § 7.4）：
//   封存段：/history/ 不可变段（浏览器 immutable + nginx proxy_cache 双层缓存）
//   尾巴：SSE snapshot.history_tail（零额外请求）；缺失/过期回退老 chart 端点只补尾巴
//   实时：tick 帧续写由图表组件自己的增量逻辑负责（阶段 2 已有，这里不管）
// 任何 /history/ 请求失败 → 整体回退老 /api/v1/chart/candles 全量（保可用性：
// 例如 nginx 尚未上线 /history/ 转发时，SPA try_files 会把请求兜给 index.html，
// resp.json() 解析失败走 catch）。
import { chartApi } from '@/api/chart'
import {
  decodeSegment, fetchSegment, fillCandles, sealedSegmentEpochs,
  SEGMENT_SECONDS, type HistoryTailMap,
} from '@/api/history'
import type { Candle, ChartInterval } from '@/types/api'

const INTERVAL_SECONDS: Record<ChartInterval, number> = {
  '10s': 10, '1m': 60, '15m': 900, '1h': 3600,
}

async function tailCandles(
  outcomeId: number, interval: ChartInterval, boundary: number, nowSec: number,
  tail: HistoryTailMap | null, tailAtSec: number | null,
): Promise<Candle[]> {
  const enc = tail?.[String(outcomeId)]?.[interval]
  // 尾巴是 snapshot 时刻的快照，之后的成交只存在于已经画上去的 tick 帧里。重载（切 tab
  // 回来 / 切 interval / 跨桶）若还用它，snapshot 之后这段会被 fillCandles 平推成横线、
  // forming 桶回到旧价（审计 P6）。只有 snapshot 距今不到 1 个 step（仍在同一桶内，
  // 缺的只会是 forming 桶本身）才复用；否则走 chart 端点补尾巴。
  const fresh = tailAtSec !== null && nowSec - tailAtSec < INTERVAL_SECONDS[interval]
  if (enc && enc.t0 === boundary && fresh) return decodeSegment(enc)
  // snapshot 尾巴缺失（SSE 未连上）/ 过期（跨过了封存边界或已陈旧）→ 只补尾巴窗口
  const resp = await chartApi.getCandles(
    outcomeId, interval,
    new Date(boundary * 1000).toISOString(), new Date(nowSec * 1000).toISOString(),
    false, 5000,
  )
  return resp?.candles ?? []
}

export async function loadHistoryCandles(
  outcomeId: number, interval: ChartInterval, lookbackMinutes: number,
  tail: HistoryTailMap | null, tailAtSec: number | null = null,
): Promise<Candle[]> {
  const nowSec = Math.floor(Date.now() / 1000)
  const fromSec = nowSec - lookbackMinutes * 60
  const step = INTERVAL_SECONDS[interval]
  const boundary = nowSec - (nowSec % SEGMENT_SECONDS[interval])
  try {
    const epochs = sealedSegmentEpochs(interval, fromSec, nowSec)
    const segs = await Promise.all(epochs.map(e => fetchSegment(outcomeId, interval, e)))
    const sealed = segs.filter((s): s is NonNullable<typeof s> => s !== null).flatMap(decodeSegment)
    const tailPart = await tailCandles(outcomeId, interval, boundary, nowSec, tail, tailAtSec)
    const all = [...sealed, ...tailPart].sort(
      (a, b) => new Date(a.t).getTime() - new Date(b.t).getTime(),
    )
    return fillCandles(all, step, fromSec, nowSec + step)   // 填到当前进行中的桶
  } catch (err) {
    console.warn('[useCandleHistory] /history/ 加载失败，回退老 chart 端点:', err)
    const resp = await chartApi.getCandles(
      outcomeId, interval,
      new Date(fromSec * 1000).toISOString(), new Date(nowSec * 1000).toISOString(),
      true, Math.max(50, Math.ceil((lookbackMinutes * 60) / step) + 8),
    )
    return resp?.candles ?? []
  }
}
