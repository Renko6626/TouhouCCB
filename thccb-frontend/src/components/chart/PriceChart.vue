<script setup lang="ts">
import { ref, onMounted, watch, onUnmounted } from 'vue'
import * as echarts from 'echarts'
import { useChartData } from '@/composables/useChartData'
import type { PricePoint } from '@/types/api'

const props = defineProps<{
  outcomeId: number
  width?: string
  height?: string
}>()

const chartData = useChartData()
const chartRef = ref<HTMLDivElement | null>(null)
let chartInstance: echarts.ECharts | null = null

const loadData = async () => {
  if (!props.outcomeId) return
  const now = new Date()
  const fromTs = new Date(now.getTime() - 24 * 60 * 60 * 1000).toISOString()
  const toTs = now.toISOString()
  await chartData.getPriceSeries(props.outcomeId, fromTs, toTs)
}

const initChart = () => {
  if (!chartRef.value) return
  if (chartInstance) {
    chartInstance.dispose()
  }

  chartInstance = echarts.init(chartRef.value)

  const option: echarts.EChartsOption = {
    tooltip: {
      trigger: 'axis',
      formatter: '{c}¥',
      borderColor: '#000',
      borderWidth: 2,
      textStyle: { color: '#000' },
      backgroundColor: '#fff',
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '3%',
      containLabel: true,
    },
    xAxis: {
      type: 'time',
      splitLine: { show: false },
      axisLine: { lineStyle: { color: '#000' } },
      axisTick: { lineStyle: { color: '#000' } },
      axisLabel: { color: '#333' },
    },
    yAxis: {
      type: 'value',
      splitLine: {
        show: true,
        lineStyle: { color: '#e0e0e0', type: 'dashed' },
      },
      axisLine: { lineStyle: { color: '#000' } },
      axisLabel: { color: '#333' },
    },
    series: [
      {
        name: '价格',
        type: 'line',
        data: [],
        smooth: false,
        symbol: 'none',
        lineStyle: { width: 2, color: '#000' },
        areaStyle: { color: '#f0f0f0' },
      },
    ],
    animation: false,
  }

  chartInstance.setOption(option)
}

const updateChart = () => {
  if (!chartInstance || !chartData.priceData.value.length) return

  const seriesData = chartData.priceData.value.map((point: PricePoint) => [
    new Date(point.ts).getTime(),
    point.price,
  ])

  chartInstance.setOption({
    series: [{ data: seriesData }],
  })
}

onMounted(() => {
  loadData()
  initChart()
})

watch(() => props.outcomeId, () => {
  loadData()
})

watch(() => chartData.priceData.value, () => {
  updateChart()
}, { immediate: true })

onUnmounted(() => {
  if (chartInstance) {
    chartInstance.dispose()
  }
})
</script>

<template>
  <div
    :style="{ width: props.width || '100%', height: props.height || '400px' }"
    class="price-chart-container"
  >
    <div v-if="chartData.loading" class="chart-state">
      <p class="chart-state-text">加载价格数据中...</p>
    </div>

    <div v-else-if="chartData.error" class="chart-state">
      <p class="chart-state-text chart-state-text--error">{{ chartData.error }}</p>
      <button @click="loadData()" class="chart-retry-btn">重试</button>
    </div>

    <div v-else-if="chartData.priceData.value.length === 0" class="chart-state">
      <p class="chart-state-text">暂无价格数据</p>
    </div>

    <div v-else ref="chartRef" style="width: 100%; height: 100%"></div>
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
  font-size: 14px;
  color: #555;
}

.chart-state-text--error {
  color: #000;
  font-weight: 600;
}

.chart-retry-btn {
  margin-top: 12px;
  padding: 4px 16px;
  background: #000;
  color: #fff;
  border: 2px solid #000;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}

.chart-retry-btn:hover {
  transform: translate(-1px, -1px);
  box-shadow: 3px 3px 0 #000;
}
</style>
