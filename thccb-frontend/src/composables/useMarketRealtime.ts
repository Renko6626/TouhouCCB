import { ref, onBeforeUnmount, watch, type InjectionKey, type Ref } from 'vue'
import { MarketStream } from '@/api/stream'
import type { MarketEvent, MarketStatusEventData, TradeEventData } from '@/types/stream'

type TradePayload = TradeEventData['trade']

interface SnapshotData {
  outcomes: Array<{ id: number; current_price: number }>
}

export interface UseMarketRealtimeReturn {
  isConnected: Ref<boolean>
  reconnectCount: Ref<number>
  // 各 outcome 当前 LMSR 价；snapshot 初始化，trade 用 market_prices_post 全量 patch
  pricesByOutcome: Ref<Map<number, number>>
  // 最新一条 trade（snapshot/market_status 不写这个）。图表 watch 此值实现增量 append
  latestTrade: Ref<TradePayload | null>
  // 最新一条 market_status 事件（halt/settled 等），TradingView watch 用于重拉 market detail
  latestMarketStatus: Ref<MarketStatusEventData | null>
  // 每次 snapshot 成功（含初次连上/重连后再次锚定）递增；charts 可监听用于完全重建
  snapshotToken: Ref<number>
  // seq 不连续时递增（含重连 snapshot 检测出的"断线期间未收到"）；charts watch 它做 silent refetch
  gapToken: Ref<number>
}

// provide/inject 注入 key —— TradingView 调 useMarketRealtime + provide，
// 子组件（PriceChart / CandleChart）inject 拿到同一实例，避免重复建 SSE
export const MarketRealtimeKey: InjectionKey<UseMarketRealtimeReturn> = Symbol('MarketRealtime')

/**
 * 市场实时流订阅 + gap 检测组合式函数。
 *
 * 与底层 MarketStream（EventSource 封装）的关系：
 *   - MarketStream 只管 SSE 连接和事件分发，不解析业务语义
 *   - useMarketRealtime 在 MarketStream 之上叠加 per-market seq 跟踪、
 *     pricesByOutcome 反应式状态、gap 检测回调
 *
 * 使用方（charts/TradingView）的契约：
 *   1) 初始数据从 REST API 拉（snapshot 也只携带 current_price，不带历史）
 *   2) 后续靠 latestTrade 增量推
 *   3) onGap 触发时做一次 silent refetch 重建状态
 */
export function useMarketRealtime(marketId: Ref<number | null>): UseMarketRealtimeReturn {
  const stream = new MarketStream()
  const isConnected = ref(false)
  const reconnectCount = ref(0)
  const pricesByOutcome = ref<Map<number, number>>(new Map())
  const latestTrade = ref<TradePayload | null>(null)
  const latestMarketStatus = ref<MarketStatusEventData | null>(null)
  const snapshotToken = ref(0)
  const gapToken = ref(0)

  // 内部状态：上一次成功处理的 event seq。0 表示尚未通过 snapshot 锚定
  let lastSeq = 0
  // outcome.id 升序数组，用于 trade.market_prices_post 索引 → outcomeId 映射
  let outcomesOrder: number[] = []

  const fireGap = (from: number, to: number) => {
    console.warn(`[realtime] gap detected: expected seq=${from}, got ${to}`)
    gapToken.value += 1
  }

  const handleOpen = () => {
    isConnected.value = true
    reconnectCount.value = stream.reconnectCount
  }

  const handleSnapshot = (evt: MarketEvent) => {
    const snap = evt.data as SnapshotData
    // 按 id 升序 outcomesOrder，确保和后端 market_prices_post 顺序一致
    const sorted = [...snap.outcomes].sort((a, b) => a.id - b.id)
    outcomesOrder = sorted.map(o => o.id)

    const next = new Map<number, number>()
    for (const o of snap.outcomes) {
      next.set(o.id, o.current_price)
    }
    pricesByOutcome.value = next

    // 重连场景：旧 lastSeq > 0 且新 snapshot.seq > 旧 lastSeq → 期间发生过事件 → 触发 gap
    if (lastSeq > 0 && evt.seq !== undefined && evt.seq > lastSeq) {
      fireGap(lastSeq + 1, evt.seq)
    }
    lastSeq = evt.seq ?? 0
    snapshotToken.value += 1
  }

  // 检测 inline gap（trade / market_status 期间序号断档）
  const checkInlineGap = (evt: MarketEvent) => {
    if (evt.seq === undefined || lastSeq === 0) return
    const expected = lastSeq + 1
    if (evt.seq !== expected) {
      fireGap(expected, evt.seq)
    }
    lastSeq = evt.seq
  }

  const handleTrade = (evt: MarketEvent) => {
    checkInlineGap(evt)
    const payload = (evt.data as TradeEventData).trade

    // 用 market_prices_post 全量 patch 所有 outcome 价（含未被交易的，LMSR 联动）
    const mpp = payload.market_prices_post
    if (mpp && outcomesOrder.length === mpp.length) {
      const next = new Map(pricesByOutcome.value)
      for (let i = 0; i < outcomesOrder.length; i++) {
        next.set(outcomesOrder[i]!, mpp[i]!)
      }
      pricesByOutcome.value = next
    } else if (mpp) {
      // outcomesOrder 长度不匹配（snapshot 未到 / 数据异常）→ 退化只 patch 被交易那个
      const next = new Map(pricesByOutcome.value)
      next.set(payload.outcome_id, payload.post_market_price)
      pricesByOutcome.value = next
    }

    latestTrade.value = payload
  }

  const handleMarketStatus = (evt: MarketEvent) => {
    checkInlineGap(evt)
    latestMarketStatus.value = evt.data as MarketStatusEventData
  }

  const handleError = () => {
    isConnected.value = false
    reconnectCount.value = stream.reconnectCount
  }

  stream.on('open', handleOpen)
  stream.on('snapshot', handleSnapshot)
  stream.on('trade', handleTrade)
  stream.on('market_status', handleMarketStatus)
  stream.on('error', handleError)

  // 监听 tab visibility：浏览器后台 throttle 会让 SSE 推送堆积或被代理 idle 掐断。
  // 回到前台超过阈值 → 触发一次 reconcile（通过 gapToken），让图表 silent refetch。
  // 阈值 3s 是经验值：短切回不必扰动，长挂回来必须重对账。
  const VISIBILITY_RECONCILE_THRESHOLD_MS = 3000
  let hiddenAt = 0
  const handleVisibility = () => {
    if (document.hidden) {
      hiddenAt = Date.now()
      return
    }
    if (hiddenAt > 0 && Date.now() - hiddenAt > VISIBILITY_RECONCILE_THRESHOLD_MS) {
      console.log('[realtime] tab visible after long hidden, triggering reconcile')
      gapToken.value += 1
    }
    hiddenAt = 0
  }
  document.addEventListener('visibilitychange', handleVisibility)

  watch(
    marketId,
    (id, oldId) => {
      if (oldId !== null && oldId !== undefined) {
        stream.disconnect()
        // 切市场：重置内部状态（避免旧市场的 lastSeq 串到新市场）
        lastSeq = 0
        outcomesOrder = []
        pricesByOutcome.value = new Map()
        latestTrade.value = null
        latestMarketStatus.value = null
      }
      if (id !== null && id !== undefined) {
        stream.connect(id)
      }
    },
    { immediate: true },
  )

  onBeforeUnmount(() => {
    stream.off('open', handleOpen)
    stream.off('snapshot', handleSnapshot)
    stream.off('trade', handleTrade)
    stream.off('market_status', handleMarketStatus)
    stream.off('error', handleError)
    stream.disconnect()
    document.removeEventListener('visibilitychange', handleVisibility)
  })

  return {
    isConnected,
    reconnectCount,
    pricesByOutcome,
    latestTrade,
    latestMarketStatus,
    snapshotToken,
    gapToken,
  }
}
