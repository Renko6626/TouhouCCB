// FX 缓存历史适配层：封存段 + SSE 尾段 + `/chart` 回退。
//
// 与 LMSR `useCandleHistory.ts` 的关系：流程同构（封存段不可变、尾段带 freshness、
// 失败回退老端点），但落实 FX 口径：
//   - 段价格/成交量是十进制字符串，解码才转 number；
//   - 段 URL 带 `history_version`，版本切换即整套重读；
//   - 后端历史未就绪（503/无版本/段缺失）时**整体回退** `/chart`，绝不用空桶
//     冒充“完整成交量”，也不会把不完整历史当作可缓存结果。
//
// `loadFxHistoryCandles` 是纯取数函数，组件 worker 直接按冻结签名调用；
// `useFxCandleHistory` 是轻量有状态封装（加载态/错误/版本/generation 防串）。
// 不建立任何 SSE 连接——实时增量由组件持有的 FxStream + FxCandleEngine 负责。

import { ref, toValue, watch, type MaybeRefOrGetter, type Ref } from 'vue'
import { fetchFxHistorySegment, fxApi } from '@/api/fx'
import type {
  FxChartPoint,
  FxHistoryInterval,
  FxHistorySnapshotTail,
  FxHistoryTail,
} from '@/types/fx'
import {
  FX_HISTORY_INTERVAL_SECONDS,
  FX_HISTORY_SEGMENT_SECONDS,
  decodeFxHistorySegment,
  fxHistorySegmentEpochs,
  mergeFxHistoryCandles,
} from '@/utils/fxHistory'
import { fxTimestampToSeconds } from '@/utils/fxCandle'

/** 默认 lookback（分钟）；组件通常按周期传入自己的窗口。 */
export const FX_HISTORY_DEFAULT_LOOKBACK_MINUTES = 1440

function isoAt(seconds: number): string {
  return new Date(seconds * 1000).toISOString()
}

/**
 * 尾段：优先用 SSE snapshot 的新鲜尾段（同一个段的正在进行部分），否则只对
 * [最后封存边界, now] 调 `/chart` 补尾巴——不重读封存段。
 */
async function loadTailPoints(
  pairId: number,
  interval: FxHistoryInterval,
  snapshotTail: FxHistorySnapshotTail | null,
  nowSec: number,
  boundary: number,
  step: number,
): Promise<FxChartPoint[]> {
  const version = snapshotTail?.history_version ?? null
  const tailAt = snapshotTail?.history_tail_at
    ? fxTimestampToSeconds(snapshotTail.history_tail_at) : null
  const fresh = tailAt !== null && nowSec - tailAt < step
  const encoded: FxHistoryTail | null = snapshotTail?.history_tail ?? null
  const tailSegment = encoded?.[interval]
  if (version && fresh && tailSegment && tailSegment.t0 === boundary) {
    return decodeFxHistorySegment(tailSegment)
  }
  return fxApi.getChart(pairId, interval, isoAt(boundary), isoAt(nowSec))
}

/**
 * 仅补尾段（gap / `history_invalidated` 时用）：不重读封存段。
 * 返回 [最后封存边界, now] 的图表点，供组件替换引擎尾部。
 */
export async function loadFxHistoryTail(
  pairId: number,
  interval: FxHistoryInterval,
  snapshotTail: FxHistorySnapshotTail | null,
): Promise<FxChartPoint[]> {
  const nowSec = Math.floor(Date.now() / 1000)
  const boundary = nowSec - (nowSec % FX_HISTORY_SEGMENT_SECONDS[interval])
  return loadTailPoints(
    pairId, interval, snapshotTail, nowSec, boundary,
    FX_HISTORY_INTERVAL_SECONDS[interval],
  )
}

/**
 * 加载 FX 图表初始数据：封存段 + 尾段，按桶合并（封存段优先）并补齐空桶。
 *
 * @param pairId         FX pair id
 * @param interval       10s/1m/15m/1h（与后端 RING_SPEC 一致）
 * @param lookbackMinutes 请求窗口（用当前时间倒推）
 * @param snapshotTail   SSE snapshot 的版本/尾段上下文；缺失或 `history_ready === false`
 *                       时直接回退 `/chart`
 * @returns 升序、连续（空桶 v=0 平推）的图表点
 *
 * 历史未就绪/段缺失/结构非法 → 抛错被捕获后回退 `/chart`，错误不会静默变成
 * “完整但全是 0 成交量”的假历史。
 */
export async function loadFxHistoryCandles(
  pairId: number,
  interval: FxHistoryInterval,
  lookbackMinutes: number,
  snapshotTail: FxHistorySnapshotTail | null,
): Promise<FxChartPoint[]> {
  const step = FX_HISTORY_INTERVAL_SECONDS[interval]
  const segmentSeconds = FX_HISTORY_SEGMENT_SECONDS[interval]
  const lookback = Number.isFinite(lookbackMinutes) ? Math.max(1, Math.floor(lookbackMinutes)) : FX_HISTORY_DEFAULT_LOOKBACK_MINUTES
  const nowSec = Math.floor(Date.now() / 1000)
  const fromSec = nowSec - lookback * 60
  const boundary = nowSec - (nowSec % segmentSeconds)
  const version = snapshotTail?.history_version ?? null
  const ready = snapshotTail !== null && snapshotTail.history_ready !== false && !!version

  if (ready && version) {
    try {
      const epochs = fxHistorySegmentEpochs(interval, fromSec, nowSec)
      const segments = await Promise.all(
        epochs.map((epoch) => fetchFxHistorySegment(pairId, version, interval, epoch)),
      )
      // 任一封存段缺失 = 历史不完整：回退 /chart，不用空桶平推冒充完整量。
      if (segments.some((segment) => segment === null)) {
        throw new Error('FX history segment unavailable')
      }
      const sealed = segments.flatMap((segment) => decodeFxHistorySegment(segment!))
      const tail = await loadTailPoints(
        pairId, interval, snapshotTail, nowSec, boundary, step,
      )
      return mergeFxHistoryCandles(sealed, tail, step, fromSec, nowSec + step)
    } catch (err) {
      console.warn('[useFxCandleHistory] /history/fx/ 加载失败，回退 /chart:', err)
    }
  }
  const raw = await fxApi.getChart(pairId, interval, isoAt(fromSec), isoAt(nowSec))
  // 回退同样补齐空桶，保持与封存段路径一致的连续形态；v=0 表示无成交而非未知。
  return mergeFxHistoryCandles([], raw, step, fromSec, nowSec + step)
}

export interface UseFxCandleHistoryParams {
  pairId: MaybeRefOrGetter<number | null>
  interval: MaybeRefOrGetter<FxHistoryInterval>
  tail: MaybeRefOrGetter<FxHistorySnapshotTail | null>
  lookbackMinutes?: MaybeRefOrGetter<number>
}

export interface UseFxCandleHistoryReturn {
  /** 最近一次成功加载的升序连续图表点 */
  points: Ref<FxChartPoint[]>
  loading: Ref<boolean>
  /** 最近一次失败；成功刷新后清空。失败时 points 保留上次成功结果 */
  error: Ref<unknown | null>
  /** 当前 points 对应的 history_version；用于组件判断是否需要重建引擎 */
  version: Ref<string | null>
  /** 完整重载（初始/切周期/gap） */
  reload: () => Promise<void>
}

/**
 * 有状态封装：pairId / interval / lookback / `history_version` 变化时自动重载，
 * 并用 generation 丢弃被后续请求取代的旧响应。任何单项失败都保留上一次成功数据，
 * 只把错误暴露给调用方（主页卡片可分别反馈）。
 */
export function useFxCandleHistory(params: UseFxCandleHistoryParams): UseFxCandleHistoryReturn {
  const points = ref<FxChartPoint[]>([])
  const loading = ref(false)
  const error = ref<unknown | null>(null)
  const version = ref<string | null>(null)
  let generation = 0

  const reload = async (): Promise<void> => {
    const pairId = toValue(params.pairId)
    const interval = toValue(params.interval)
    const tail = toValue(params.tail)
    const lookback = toValue(params.lookbackMinutes) ?? FX_HISTORY_DEFAULT_LOOKBACK_MINUTES
    if (pairId === null || pairId === undefined || pairId <= 0) {
      points.value = []
      version.value = null
      error.value = null
      return
    }
    const gen = ++generation
    loading.value = true
    try {
      const next = await loadFxHistoryCandles(pairId, interval, lookback, tail)
      if (gen !== generation) return
      points.value = next
      version.value = tail?.history_version ?? null
      error.value = null
    } catch (err) {
      if (gen !== generation) return
      error.value = err
      console.error('[useFxCandleHistory] load failed:', err)
    } finally {
      if (gen === generation) loading.value = false
    }
  }

  watch(
    () => [
      toValue(params.pairId),
      toValue(params.interval),
      toValue(params.lookbackMinutes) ?? FX_HISTORY_DEFAULT_LOOKBACK_MINUTES,
      toValue(params.tail)?.history_version ?? null,
    ],
    () => { void reload() },
    { immediate: true },
  )

  return { points, loading, error, version, reload }
}
