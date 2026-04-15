<script setup lang="ts">
import { ref, onMounted, watch, onUnmounted, nextTick } from 'vue'
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
  // 数据回来后初始化或更新图表
  await nextTick()
  if (!chartInstance) {
    initChart()
  }
  updateChart()
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
      formatter: (params: any) => {
        const p = Array.isArray(params) ? params[0] : params
        if (!p) return ''
        const date = new Date(p.value[0])
        const timeStr = date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
        return `${timeStr}<br/>价格: ¥${p.value[1].toFixed(4)}`
      },
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
      scale: true,
      splitLine: {
        show: true,
        lineStyle: { color: '#e0e0e0', type: 'dashed' },
      },
      axisLine: { lineStyle: { color: '#000' } },
      axisLabel: {
        color: '#333',
        formatter: (v: number) => v.toFixed(2),
      },
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
})

watch(() => props.outcomeId, () => {
  // 切换 outcome 时重新加载
  if (chartInstance) {
    chartInstance.dispose()
    chartInstance = null
  }
  loadData()
})

// 窗口 resize 时重新调整图表大小
const handleResize = () => chartInstance?.resize()
onMounted(() => window.addEventListener('resize', handleResize))

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  if (chartInstance) {
    chartInstance.dispose()
    chartInstance = null
  }
})
</script>

<template>
  <div
    :style="{ width: props.width || '100%', height: props.height || '400px' }"
    class="price-chart-container"
  >
    <div v-if="chartData.loading.value" class="chart-state">
      <p class="chart-state-text">加载价格数据中...</p>
    </div>

    <div v-else-if="chartData.error.value" class="chart-state">
      <p class="chart-state-text chart-state-text--error">{{ chartData.error.value }}</p>
      <button @click="loadData()" class="chart-retry-btn">重试</button>
    </div>

    <div v-else-if="chartData.priceData.value.length === 0" class="chart-state">
      <p class="chart-state-text">暂无价格数据</p>
    </div>

    <!-- chartRef 始终渲染，用 v-show 控制可见性 -->
    <div v-show="chartData.priceData.value.length > 0" ref="chartRef" style="width: 100%; height: 100%"></div>
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
