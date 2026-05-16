import api from './index'
import type { CandleSeriesResponse } from '@/types/api'

export const chartApi = {
  // K线数据
  async getCandles(
    outcomeId: number,
    interval: '10s' | '1m' | '15m' | '1h',
    fromTs: string,
    toTs: string,
    fill: boolean = false,
    limit: number = 5000,
  ): Promise<CandleSeriesResponse> {
    const params: any = {
      outcome_id: outcomeId,
      interval,
      from_ts: fromTs,
      to_ts: toTs,
      fill,
      limit,
    }
    return api.get<CandleSeriesResponse>('/api/v1/chart/candles', { params })
  },
}

export default chartApi
