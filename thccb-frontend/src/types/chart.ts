// 图表相关类型定义

export interface PricePoint {
  ts: string
  price: number
}

export interface Candle {
  t: string
  o: number
  h: number
  l: number
  c: number
  v: number
  n: number
}

export interface PriceSeriesResponse {
  outcome_id: number
  from_ts: string
  to_ts: string
  points: PricePoint[]
}

export interface CandleSeriesResponse {
  outcome_id: number
  interval: '10s' | '30s' | '1m' | '5m' | '15m' | '1h' | '1d'
  from_ts: string
  to_ts: string
  candles: Candle[]
}