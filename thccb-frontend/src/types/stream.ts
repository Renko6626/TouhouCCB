// 实时流事件类型定义

export interface MarketEvent {
  type: 'snapshot' | 'trade' | 'market_status' | 'ping' | 'tick'
  market_id: number
  ts: string
  data: any
  // per-market 单调递增序号。客户端用 seq 检测 gap：若 event.seq !== lastSeq+1
  // → 触发 silent reconcile 拉最新数据。snapshot 携带当前 seq 作为锚点。
  // ping 不递增；老后端不带这个字段 → undefined，前端跳过 gap 检测以保兼容。
  seq?: number
}

export interface TradeEventData {
  trade: {
    id: number
    type: 'buy' | 'sell'
    outcome_id: number
    username: string
    shares: number
    price: number
    gross: number
    fee: number
    post_market_price: number
    // 全市场所有 outcome 的 LMSR post 价快照，按 outcome.id 升序。
    // LMSR 单选项交易会影响所有 outcome 的价格，前端用这个 O(1) patch
    // 所有 outcome 的当前价并驱动图表增量更新。
    market_prices_post: number[]
    timestamp: string
  }
}

export interface MarketStatusEventData {
  status: 'trading' | 'halt' | 'settled'
  winning_outcome_id?: number
  settled_at?: string
}

// 8 Hz 定频帧（阶段 2）：价格向量 + 帧窗口内逐笔成交 + 市场状态。
// 迁移期与老 trade/market_status 事件共用同一 seq 计数器（每事件 +1）；
// legacy_trade_events 关掉后退化为"每帧 +1"。
export interface TickFrameData {
  status: 'trading' | 'halt' | 'settled'
  /** 全 outcome 当前价，按 outcome.id 升序，8 位小数（服务端权威精度） */
  prices: number[]
  /** 帧窗口内逐笔成交，形状与老 trade 事件的 data.trade 逐字段一致；可为空数组 */
  trades: TradeEventData['trade'][]
  /** 仅 settled 帧携带 */
  settlement?: { winning_outcome_id: number; settled_at: string }
}