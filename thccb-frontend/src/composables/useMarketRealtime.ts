import { ref, onBeforeUnmount, watch, type InjectionKey, type Ref } from 'vue'
import { MarketStream } from '@/api/stream'
import type { MarketEvent, MarketStatusEventData, TickFrameData, TradeEventData } from '@/types/stream'
import { reportServerBuild } from '@/composables/useBuildVersion'
import type { HistoryTailMap } from '@/api/history'

type TradePayload = TradeEventData['trade']

interface SnapshotData {
  status?: string
  // build 版本自刷机制（阶段 2）：后端 snapshot 携带自己的 build sha，供比对
  frontend_build?: string
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
  // 最新一帧 8Hz tick 帧（阶段 2）：价格向量+帧窗口内逐笔成交+状态。消费方 watch 此值增量更新
  latestTick: Ref<TickFrameData | null>
  // outcome.id 升序数组，供消费方把 tick 帧的价格/market_prices_post 索引换回 outcomeId
  outcomesOrder: Ref<number[]>
  // 收到首个 tick 帧后置 true；此后 legacy trade/market_status 只参与 seq 连续性、不再更新状态
  tickSeen: Ref<boolean>
  // snapshot 首包携带的历史尾巴（最后封存边界 → now），图表初始化用（阶段 4）
  historyTail: Ref<HistoryTailMap | null>
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
  const latestTick = ref<TickFrameData | null>(null)
  const outcomesOrderRef = ref<number[]>([])
  const tickSeen = ref(false)
  const historyTail = ref<HistoryTailMap | null>(null)
  let lastFrameStatus: string | null = null

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
    outcomesOrderRef.value = outcomesOrder
    lastFrameStatus = snap.status ?? null

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
    historyTail.value = (evt.data as { history_tail?: HistoryTailMap }).history_tail ?? null
    snapshotToken.value += 1
    reportServerBuild(snap.frontend_build)
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

  const handleTick = (evt: MarketEvent) => {
    tickSeen.value = true
    checkInlineGap(evt)
    const frame = evt.data as TickFrameData

    // 价格向量全量 patch（帧价格是服务端 8dp 权威值；空数组 = 老路径纯状态帧，跳过）
    if (frame.prices.length && frame.prices.length === outcomesOrder.length) {
      const next = new Map<number, number>()
      for (let i = 0; i < outcomesOrder.length; i++) {
        next.set(outcomesOrder[i]!, frame.prices[i]!)
      }
      pricesByOutcome.value = next
    }

    // 状态变更并入帧（spec § 5.1）：变化时喂给既有 latestMarketStatus 消费方
    if (frame.status !== lastFrameStatus) {
      lastFrameStatus = frame.status
      latestMarketStatus.value = {
        status: frame.status,
        ...(frame.settlement ?? {}),
      } as MarketStatusEventData
    }

    latestTick.value = frame
  }

  const handleTrade = (evt: MarketEvent) => {
    checkInlineGap(evt)
    if (tickSeen.value) return   // tick 帧已接管状态更新；老事件只参与 seq 连续性（双发防重）
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
    if (tickSeen.value) return   // tick 帧已接管状态更新；老事件只参与 seq 连续性（双发防重）
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
  stream.on('tick', handleTick)
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
    // 后台期间断线（退避中）→ 回前台立即重连，不等退避计时
    stream.reconnectNow()
  }
  document.addEventListener('visibilitychange', handleVisibility)
  const handleOnline = () => stream.reconnectNow()
  window.addEventListener('online', handleOnline)

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
        latestTick.value = null
        tickSeen.value = false
        lastFrameStatus = null
        outcomesOrderRef.value = []
        historyTail.value = null
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
    stream.off('tick', handleTick)
    stream.off('error', handleError)
    stream.disconnect()
    document.removeEventListener('visibilitychange', handleVisibility)
    window.removeEventListener('online', handleOnline)
  })

  return {
    isConnected,
    reconnectCount,
    pricesByOutcome,
    latestTrade,
    latestMarketStatus,
    snapshotToken,
    gapToken,
    latestTick,
    outcomesOrder: outcomesOrderRef,
    tickSeen,
    historyTail,
  }
}
