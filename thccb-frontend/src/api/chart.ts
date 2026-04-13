import api from './index'
import type {
  PriceSeriesResponse,
  CandleSeriesResponse
} from '@/types/api'

export const chartApi = {
  // 价格曲线
  async getPriceSeries(
    outcomeId: number,
    fromTs: string,
    toTs: string,
    limit: number = 5000,
    bucket?: string
  ): Promise<PriceSeriesResponse> {
    const params: any = {
      outcome_id: outcomeId,
      from_ts: fromTs,
      to_ts: toTs,
      limit
    }
    
    if (bucket) {
      params.bucket = bucket
    }
    
    return api.get<PriceSeriesResponse>('/api/v1/chart/price', { params })
  },

  // K线数据
  async getCandles(
    outcomeId: number,
    interval: '10s' | '30s' | '1m' | '5m' | '15m' | '1h' | '1d',
    fromTs: string,
    toTs: string,
    fill: boolean = false,
    limit: number = 5000,
    maxTrades: number = 200000
  ): Promise<CandleSeriesResponse> {
    const params: any = {
      outcome_id: outcomeId,
      interval,
      from_ts: fromTs,
      to_ts: toTs,
      fill,
      limit,
      max_trades: maxTrades
    }
    
    return api.get<CandleSeriesResponse>('/api/v1/chart/candles', { params })
  }
}

export default chartApi