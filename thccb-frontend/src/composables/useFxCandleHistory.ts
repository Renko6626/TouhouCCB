// FX 缓存历史适配层：封存段 + SSE 尾段 + `/chart` 回退。
//
// 与 LMSR `useCandleHistory.ts` 的关系：流程同构（封存段不可变、尾段带 freshness、
// 失败回退老端点），但落实 FX 口径：
//   - 段价格/成交量是十进制字符串，解码才转 number；
//   - 段 URL 带 `history_version`，版本切换即整套重读；
//   - 后端历史未就绪 / 段缺失 / 段结构非法时回退 `/chart`，且**结果必须带明确的
//     覆盖游标**：元数据缺失时显式报错（safe retry），绝不拿旧 SSE 游标假装保护了新数据。
//
// `loadFxHistoryResult` 返回 `{points, throughTradeId, historyVersion}`；旧的
// `loadFxHistoryCandles`（只取点）保留兼容。`useFxCandleHistory` 是轻量有状态封装。
// 本文件不建立任何 SSE 连接——实时增量由组件持有的 FxStream + FxCandleEngine 负责。

import { ref, toValue, watch, type MaybeRefOrGetter, type Ref } from 'vue'
import { fetchFxHistorySegment, getChartWithMeta } from '@/api/fx'
import type {
  FxChartPoint,
  FxHistoryInterval,
  FxHistorySegment,
  FxHistorySnapshotTail,
} from '@/types/fx'
import {
  FX_HISTORY_INTERVAL_SECONDS,
  FX_HISTORY_MAX_BUCKETS,
  FX_HISTORY_SEGMENT_SECONDS,
  decodeFxHistorySegment,
  fxHistorySegmentEpochs,
  mergeFxHistoryCandles,
} from '@/utils/fxHistory'
import { fxTimestampToSeconds } from '@/utils/fxCandle'

/** 默认 lookback（分钟）；组件通常按周期传入自己的窗口。 */
export const FX_HISTORY_DEFAULT_LOOKBACK_MINUTES = 1440

/** 封存段并发取数上限，避免一次打开几百个请求。 */
export const FX_HISTORY_FETCH_CONCURRENCY = 6

/** 历史加载结果：点 + 结果**实际覆盖到**的成交游标与版本。 */
export interface FxHistoryLoadResult {
  points: FxChartPoint[]
  /** 实时成交 id <= 此值必须跳过（来自尾段覆盖或 `/chart` 响应头） */
  throughTradeId: number
  historyVersion: string
}

function isoAt(seconds: number): string {
  return new Date(seconds * 1000).toISOString()
}

function validThroughTradeId(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null
}

/** 有界并发取段；结果保持与 epochs 同序。 */
async function fetchSegmentsBounded(
  epochs: readonly number[],
  fetchOne: (epoch: number) => Promise<FxHistorySegment | null>,
): Promise<Array<FxHistorySegment | null>> {
  const out = new Array<FxHistorySegment | null>(epochs.length)
  for (let i = 0; i < epochs.length; i += FX_HISTORY_FETCH_CONCURRENCY) {
    const chunk = epochs.slice(i, i + FX_HISTORY_FETCH_CONCURRENCY)
    const results = await Promise.all(chunk.map((epoch) => fetchOne(epoch)))
    for (let j = 0; j < results.length; j++) out[i + j] = results[j]!
  }
  return out
}

/** 当前 forming 尾段的封存边界与是否新鲜。 */
function snapshotTailStatus(
  interval: FxHistoryInterval,
  snapshotTail: FxHistorySnapshotTail | null,
  nowSec: number,
  boundary: number,
): { segment: FxHistorySegment | null; coverage: number | null; fresh: boolean } {
  const step = FX_HISTORY_INTERVAL_SECONDS[interval]
  const tailAt = snapshotTail?.history_tail_at
    ? fxTimestampToSeconds(snapshotTail.history_tail_at) : null
  const segment = snapshotTail?.history_tail?.[interval] ?? null
  const fresh = tailAt !== null && nowSec - tailAt < step && segment !== null && segment.t0 === boundary
  return { segment, coverage: snapshotTail?.history_tail_through_trade_id ?? null, fresh }
}

/**
 * 仅补尾段（gap / `history_invalidated` 时用）：不重读封存段。
 * 返回 [最后封存边界, now] 的点 + 对应的覆盖游标（snapshot 尾段或 `/chart` 响应头）。
 */
export async function loadFxHistoryTailResult(
  pairId: number,
  interval: FxHistoryInterval,
  snapshotTail: FxHistorySnapshotTail | null,
): Promise<FxHistoryLoadResult> {
  const nowSec = Math.floor(Date.now() / 1000)
  const boundary = nowSec - (nowSec % FX_HISTORY_SEGMENT_SECONDS[interval])
  const version = snapshotTail?.history_version ?? null
  const { segment, coverage, fresh } = snapshotTailStatus(interval, snapshotTail, nowSec, boundary)
  if (version && fresh && segment) {
    const throughTradeId = validThroughTradeId(coverage)
    if (throughTradeId === null) throw new Error('FX history tail is missing through-trade-id')
    return { points: decodeFxHistorySegment(segment), throughTradeId, historyVersion: version }
  }
  const meta = await getChartWithMeta(pairId, interval, isoAt(boundary), isoAt(nowSec))
  return { points: meta.points, throughTradeId: meta.throughTradeId, historyVersion: meta.historyVersion }
}

/** 兼容包装：只要点。 */
export async function loadFxHistoryTail(
  pairId: number,
  interval: FxHistoryInterval,
  snapshotTail: FxHistorySnapshotTail | null,
): Promise<FxChartPoint[]> {
  return (await loadFxHistoryTailResult(pairId, interval, snapshotTail)).points
}

/**
 * 加载 FX 图表初始数据并返回**结果游标**：封存段 + 尾段，按桶合并（封存段优先）并补齐空桶。
 *
 * @param pairId         FX pair id
 * @param interval       10s/1m/15m/1h（与后端 RING_SPEC 一致）
 * @param lookbackMinutes 请求窗口（用当前时间倒推，自动裁剪到 20,000 桶内）
 * @param snapshotTail   SSE snapshot 的版本/尾段上下文
 * @returns `{points, throughTradeId, historyVersion}`
 *
 * 游标规则：
 * - 使用 snapshot 新鲜尾段（封存段路径）→ 游标 = `history_tail_through_trade_id`；
 * - 任何 `/chart` 回退（尾段过期 / 段缺失 / 历史未就绪）→ 游标 = 该 HTTP 响应的
 *   `X-FX-Through-Trade-ID`，绝不复用更旧的 SSE 游标；
 * - `/chart` 响应缺元数据 → 显式抛错（safe retry），不假装旧游标仍有效。
 */
export async function loadFxHistoryResult(
  pairId: number,
  interval: FxHistoryInterval,
  lookbackMinutes: number,
  snapshotTail: FxHistorySnapshotTail | null,
): Promise<FxHistoryLoadResult> {
  const step = FX_HISTORY_INTERVAL_SECONDS[interval]
  const segmentSeconds = FX_HISTORY_SEGMENT_SECONDS[interval]
  const lookback = Number.isFinite(lookbackMinutes)
    ? Math.max(1, Math.floor(lookbackMinutes)) : FX_HISTORY_DEFAULT_LOOKBACK_MINUTES
  const nowSec = Math.floor(Date.now() / 1000)
  const requestedFromSec = nowSec - lookback * 60
  // 对齐后端 /chart 的 20,000 桶上限：超限时只用最近窗口，避免无界合并/请求。
  const lastBucket = nowSec - (nowSec % step)
  let fromSec = requestedFromSec
  if ((lastBucket - (fromSec - (fromSec % step))) / step + 1 > FX_HISTORY_MAX_BUCKETS) {
    fromSec = lastBucket - (FX_HISTORY_MAX_BUCKETS - 1) * step
    console.warn(`[useFxCandleHistory] lookback 超出 ${FX_HISTORY_MAX_BUCKETS} 桶，已裁剪到最近窗口`)
  }
  const boundary = nowSec - (nowSec % segmentSeconds)
  const version = snapshotTail?.history_version ?? null
  const ready = snapshotTail !== null && snapshotTail.history_ready !== false && !!version

  if (ready && version) {
    try {
      const epochs = fxHistorySegmentEpochs(interval, fromSec, nowSec)
      const segments = await fetchSegmentsBounded(
        epochs, (epoch) => fetchFxHistorySegment(pairId, version, interval, epoch),
      )
      // 任一封存段缺失 = 历史不完整：回退 /chart，不用空桶平推冒充完整量。
      if (segments.some((segment) => segment === null)) {
        throw new Error('FX history segment unavailable')
      }
      const sealed = segments.flatMap((segment) => decodeFxHistorySegment(segment!))
      const { segment: tailSegment, coverage, fresh } = snapshotTailStatus(interval, snapshotTail, nowSec, boundary)
      if (fresh && tailSegment) {
        const throughTradeId = validThroughTradeId(coverage)
        if (throughTradeId === null) throw new Error('FX history tail is missing through-trade-id')
        return {
          points: mergeFxHistoryCandles(sealed, decodeFxHistorySegment(tailSegment), step, fromSec, nowSec + step),
          throughTradeId,
          historyVersion: version,
        }
      }
      // 尾段不可复用：只取 [封存边界, now] 的 /chart，游标以该 HTTP 响应为准
      const meta = await getChartWithMeta(pairId, interval, isoAt(boundary), isoAt(nowSec))
      return {
        points: mergeFxHistoryCandles(sealed, meta.points, step, fromSec, nowSec + step),
        throughTradeId: meta.throughTradeId,
        historyVersion: meta.historyVersion,
      }
    } catch (err) {
      console.warn('[useFxCandleHistory] /history/fx/ 不可用，回退 /chart:', err)
    }
  }
  const meta = await getChartWithMeta(pairId, interval, isoAt(fromSec), isoAt(nowSec))
  return {
    points: mergeFxHistoryCandles([], meta.points, step, fromSec, nowSec + step),
    throughTradeId: meta.throughTradeId,
    historyVersion: meta.historyVersion,
  }
}

/**
 * 兼容包装（冻结签名）：只返回点。需要精确覆盖游标时用 `loadFxHistoryResult`。
 */
export async function loadFxHistoryCandles(
  pairId: number,
  interval: FxHistoryInterval,
  lookbackMinutes: number,
  snapshotTail: FxHistorySnapshotTail | null,
): Promise<FxChartPoint[]> {
  return (await loadFxHistoryResult(pairId, interval, lookbackMinutes, snapshotTail)).points
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
  /** 最近一次结果的 history_version（可能是 /chart 响应头里的版本） */
  version: Ref<string | null>
  /** 最近一次结果覆盖到的成交 id；组件据此设置引擎 coverage */
  throughTradeId: Ref<number | null>
  /** 完整重载（初始/切周期/gap） */
  reload: () => Promise<void>
}

/**
 * 有状态封装：pairId / interval / lookback / `history_version` 变化时自动重载，
 * 并用 generation 丢弃被后续请求取代的旧响应。任何单项失败都保留上一次成功数据。
 *
 * 关键：watch 用**独立 primitive getter**，同一版本下替换 `tail` 对象不会触发重载，
 * 避免重复请求封存段。
 */
export function useFxCandleHistory(params: UseFxCandleHistoryParams): UseFxCandleHistoryReturn {
  const points = ref<FxChartPoint[]>([])
  const loading = ref(false)
  const error = ref<unknown | null>(null)
  const version = ref<string | null>(null)
  const throughTradeId = ref<number | null>(null)
  let generation = 0

  const reload = async (): Promise<void> => {
    const pairId = toValue(params.pairId)
    const interval = toValue(params.interval)
    const tail = toValue(params.tail)
    const lookback = toValue(params.lookbackMinutes) ?? FX_HISTORY_DEFAULT_LOOKBACK_MINUTES
    if (pairId === null || pairId === undefined || pairId <= 0) {
      points.value = []
      version.value = null
      throughTradeId.value = null
      error.value = null
      return
    }
    const gen = ++generation
    loading.value = true
    try {
      const result = await loadFxHistoryResult(pairId, interval, lookback, tail)
      if (gen !== generation) return
      points.value = result.points
      version.value = result.historyVersion
      throughTradeId.value = result.throughTradeId
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
    [
      () => toValue(params.pairId),
      () => toValue(params.interval),
      () => toValue(params.lookbackMinutes) ?? FX_HISTORY_DEFAULT_LOOKBACK_MINUTES,
      () => toValue(params.tail)?.history_version ?? null,
    ],
    () => { void reload() },
    { immediate: true },
  )

  return { points, loading, error, version, throughTradeId, reload }
}
