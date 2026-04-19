import { ref, computed } from 'vue'
import { chartApi } from '@/api/chart'
import type { PriceSeriesResponse, CandleSeriesResponse, PricePoint, Candle } from '@/types/api'

/**
 * 图表数据获取和缓存组合式函数
 * 封装图表API调用，提供数据缓存和防抖功能
 */
export function useChartData() {
  const loading = ref(false)
  const error = ref<string | null>(null)
  const priceData = ref<PricePoint[]>([])
  const candleData = ref<Candle[]>([])

  // 获取价格序列数据
  const getPriceSeries = async (
    outcomeId: number,
    fromTs: string,
    toTs: string,
    limit: number = 5000,
    bucket?: string
  ): Promise<PriceSeriesResponse | null> => {
    loading.value = true
    error.value = null
    
    try {
      const response = await chartApi.getPriceSeries(
        outcomeId,
        fromTs,
        toTs,
        limit,
        bucket
      )
      priceData.value = response.points
      return response
    } catch (err: any) {
      error.value = err.message || '获取价格数据失败'
      console.error('获取价格数据失败:', err)
      return null
    } finally {
      loading.value = false
    }
  }

  // 获取K线数据
  const getCandles = async (
    outcomeId: number,
    interval: '10s' | '30s' | '1m' | '5m' | '15m' | '1h' | '1d',
    fromTs: string,
    toTs: string,
    fill: boolean = false,
    limit: number = 5000,
    maxTrades: number = 200000,
    silent: boolean = false
  ): Promise<CandleSeriesResponse | null> => {
    if (!silent) {
      loading.value = true
    }
    error.value = null
    
    try {
      const response = await chartApi.getCandles(
        outcomeId,
        interval,
        fromTs,
        toTs,
        fill,
        limit,
        maxTrades
      )
      candleData.value = response.candles
      return response
    } catch (err: any) {
      error.value = err.message || '获取K线数据失败'
      console.error('获取K线数据失败:', err)
      return null
    } finally {
      if (!silent) {
        loading.value = false
      }
    }
  }

  // 计算价格变化率
  const calculatePriceChangeRate = (points: PricePoint[]): number => {
    if (points.length < 2) return 0
    const first = points[0]!.price
    const last = points[points.length - 1]!.price
    return ((last - first) / first * 100)
  }

  // 计算K线统计信息
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
      change
    }
  }

  return {
    loading,
    error,
    priceData,
    candleData,
    getPriceSeries,
    getCandles,
    calculatePriceChangeRate,
    getCandleStats,
  }
}

// 类型导出
export type UseChartDataReturn = ReturnType<typeof useChartData>