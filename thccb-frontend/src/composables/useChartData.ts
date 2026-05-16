import { ref } from 'vue'
import { chartApi } from '@/api/chart'
import type { CandleSeriesResponse, Candle } from '@/types/api'

/**
 * 图表数据获取和缓存组合式函数
 * 封装图表 API 调用，提供 K 线数据缓存
 */
export function useChartData() {
  const loading = ref(false)
  const error = ref<string | null>(null)
  const candleData = ref<Candle[]>([])

  // 获取 K 线数据
  const getCandles = async (
    outcomeId: number,
    interval: '10s' | '1m' | '15m' | '1h',
    fromTs: string,
    toTs: string,
    fill: boolean = false,
    limit: number = 5000,
    silent: boolean = false,
  ): Promise<CandleSeriesResponse | null> => {
    if (!silent) loading.value = true
    error.value = null

    try {
      const response = await chartApi.getCandles(
        outcomeId, interval, fromTs, toTs, fill, limit,
      )
      candleData.value = response.candles
      return response
    } catch (err: any) {
      error.value = err.message || '获取K线数据失败'
      console.error('获取K线数据失败:', err)
      return null
    } finally {
      if (!silent) loading.value = false
    }
  }

  // 计算 K 线统计信息
  const getCandleStats = (candles: Candle[]): {
    volume: number
    high: number
    low: number
    change: number
  } => {
    if (candles.length === 0) {
      return { volume: 0, high: 0, low: 0, change: 0 }
    }

    const first = candles[0]!
    const last = candles[candles.length - 1]!

    let totalVolume = 0
    let highestHigh = first.h
    let lowestLow = first.l

    candles.forEach(candle => {
      totalVolume += candle.v
      if (candle.h > highestHigh) highestHigh = candle.h
      if (candle.l < lowestLow) lowestLow = candle.l
    })

    const change = ((last.c - first.o) / first.o * 100)

    return {
      volume: totalVolume,
      high: highestHigh,
      low: lowestLow,
      change,
    }
  }

  return {
    loading,
    error,
    candleData,
    getCandles,
    getCandleStats,
  }
}

// 类型导出
export type UseChartDataReturn = ReturnType<typeof useChartData>
