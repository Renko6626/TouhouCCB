// 实时流事件类型定义

export interface MarketEvent {
  type: 'snapshot' | 'trade' | 'market_status' | 'ping'
  market_id: number
  ts: string
  data: any
}

export interface TradeEventData {
  trade: {
    type: 'buy' | 'sell'
    outcome_id: number
    shares: number
    price: number
    timestamp: string
  }
}

export interface MarketStatusEventData {
  status: 'trading' | 'halt' | 'settled'
  winning_outcome_id?: number
  settled_at?: string
}