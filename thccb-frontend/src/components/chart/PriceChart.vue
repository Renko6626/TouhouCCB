<script setup lang="ts">
import { ref, onMounted, watch, onUnmounted, nextTick, computed } from 'vue'
import {
  createChart,
  ColorType,
  AreaSeries,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useChartData } from '@/composables/useChartData'
import type { PricePoint } from '@/types/api'

const props = defineProps<{
  outcomeId: number
  width?: string
  height?: string
}>()

const chartData = useChartData()
const chartRef = ref<HTMLDivElement | null>(null)
let chartInstance: IChartApi | null = null
let areaSeries: ISeriesApi<'Area', Time> | null = null
let resizeObserver: ResizeObserver | null = null

// 价格涨跌判断
const priceDirection = computed(() => {
  const pts = chartData.priceData.value
  if (pts.length < 2) return 'neutral'
  return pts[pts.length - 1].price >= pts[0].price ? 'up' : 'down'
})

const COLORS = {
  up: { line: '#16a34a', topFill: 'rgba(22,163,74,0.25)', bottomFill: 'rgba(22,163,74,0.02)' },
  down: { line: '#dc2626', topFill: 'rgba(220,38,38,0.25)', bottomFill: 'rgba(220,38,38,0.02)' },
  neutral: { line: '#000000', topFill: 'rgba(0,0,0,0.12)', bottomFill: 'rgba(0,0,0,0.02)' },
}

const loadData = async () => {
  if (!props.outcomeId) return
  const now = new Date()
  const fromTs = new Date(now.getTime() - 24 * 60 * 60 * 1000).toISOString()
  const toTs = now.toISOString()
  await chartData.getPriceSeries(props.outcomeId, fromTs, toTs)
  await nextTick()
  if (!chartInstance) {
    initChart()
  }
  updateChart()
}

const initChart = () => {
  if (!chartRef.value) return
  if (chartInstance) {
    chartInstance.remove()
    chartInstance = null
  }

  chartInstance = createChart(chartRef.value, {
    layout: {
      background: { type: ColorType.Solid, color: '#ffffff' },
      textColor: '#333',
    },
    grid: {
      vertLines: { color: '#f0f0f0' },
      horzLines: { color: '#f0f0f0' },
    },
    rightPriceScale: {
      borderColor: '#e0e0e0',
      scaleMargins: { top: 0.1, bottom: 0.05 },
    },
    timeScale: {
      borderColor: '#e0e0e0',
      timeVisible: true,
      secondsVisible: false,
    },
    crosshair: { mode: 0 },
    width: chartRef.value.clientWidth,
    height: chartRef.value.clientHeight,
  })

  const c = COLORS[priceDirection.value]
  areaSeries = chartInstance.addSeries(AreaSeries, {
    lineColor: c.line,
    lineWidth: 2,
    topColor: c.topFill,
    bottomColor: c.bottomFill,
    crosshairMarkerRadius: 4,
    crosshairMarkerBorderWidth: 2,
    crosshairMarkerBorderColor: c.line,
    crosshairMarkerBackgroundColor: '#fff',
    priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
  })
}

const updateChart = () => {
  if (!areaSeries || !chartData.priceData.value.length) return

  const data = chartData.priceData.value.map((pt: PricePoint) => ({
    time: Math.floor(new Date(pt.ts).getTime() / 1000) as UTCTimestamp,
    value: pt.price,
  }))

  areaSeries.setData(data)

  // 更新涨跌颜色
  const c = COLORS[priceDirection.value]
  areaSeries.applyOptions({
    lineColor: c.line,
    topColor: c.topFill,
    bottomColor: c.bottomFill,
    crosshairMarkerBorderColor: c.line,
  })

  chartInstance?.timeScale().fitContent()
}

const setupResize = () => {
  if (!chartRef.value || !chartInstance) return
  resizeObserver = new ResizeObserver((entries) => {
    const e = entries[0]
    if (!e) return
    chartInstance?.applyOptions({
      width: e.contentRect.width,
      height: e.contentRect.height,
    })
  })
  resizeObserver.observe(chartRef.value)
}

onMounted(() => {
  loadData()
})

watch(() => props.outcomeId, () => {
  if (chartInstance) {
    chartInstance.remove()
    chartInstance = null
    areaSeries = null
  }
  loadData()
})

onUnmounted(() => {
  if (resizeObserver) { resizeObserver.disconnect(); resizeObserver = null }
  if (chartInstance) { chartInstance.remove(); chartInstance = null }
})
</script>

<template>
  <div
    :style="{ width: props.width || '100%', height: props.height || '400px' }"
    class="price-chart-container"
  >
    <div v-if="chartData.loading.value" class="chart-state">
      <p class="chart-state-text">加载中...</p>
    </div>

    <div v-else-if="chartData.error.value" class="chart-state">
      <p class="chart-state-text chart-state-text--error">{{ chartData.error.value }}</p>
      <button @click="loadData()" class="chart-retry-btn">重试</button>
    </div>

    <div v-else-if="chartData.priceData.value.length === 0" class="chart-state">
      <p class="chart-state-text">暂无价格数据</p>
    </div>

    <div v-show="chartData.priceData.value.length > 0" ref="chartRef" style="width:100%;height:100%"></div>
  </div>
</template>

<style scoped>
.price-chart-container {
  position: relative;
}

.chart-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  min-height: 200px;
}

.chart-state-text {
  font-size: 13px;
  color: #888;
}

.chart-state-text--error {
  color: #000;
  font-weight: 600;
}

.chart-retry-btn {
  margin-top: 10px;
  padding: 4px 14px;
  background: #000;
  color: #fff;
  border: 2px solid #000;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}
</style>
