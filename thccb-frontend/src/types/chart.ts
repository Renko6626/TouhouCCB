// 图表相关类型定义

/** 前端支持的 candle interval —— 跟后端 OutcomeCandle.interval CheckConstraint 对齐。
 * 30s/5m/1d 是后端 rollup-compatible，但前端不暴露主动发送。 */
export type ChartInterval = '10s' | '1m' | '15m' | '1h'

export interface Candle {
  t: string
  o: number
  h: number
  l: number
  c: number
  v: number
  n: number
}

export interface CandleSeriesResponse {
  outcome_id: number
  interval: ChartInterval
  from_ts: string
  to_ts: string
  candles: Candle[]
}
