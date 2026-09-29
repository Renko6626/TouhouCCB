<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  AreaSeries,
  ColorType,
  LineStyle,
  TickMarkType,
  type IChartApi,
  type Time,
} from 'lightweight-charts'
import { getPalette, withAlpha } from '@/utils/palette'
import { makeDemoCandles, movingAverage, type DemoOutcome, type DemoInterval } from './data'

const props = defineProps<{
  outcome: DemoOutcome
  interval: DemoInterval
  mode: 'candle' | 'line'
}>()
const container = ref<HTMLDivElement | null>(null)
let chart: IChartApi | null = null

function draw() {
  if (!container.value) return
  chart?.remove()
  const palette = getPalette()
  const directionColor = props.outcome.change >= 0 ? palette.up : palette.down
  chart = createChart(container.value, {
    autoSize: true,
    layout: {
      background: { type: ColorType.Solid, color: '#ffffff' },
      textColor: '#666666',
      fontSize: 11,
    },
    grid: {
      vertLines: { color: '#f0f0f0' },
      horzLines: { color: '#ededed', style: LineStyle.Dashed },
    },
    rightPriceScale: { borderColor: '#dddddd', scaleMargins: { top: 0.09, bottom: 0.26 } },
    timeScale: {
      borderColor: '#dddddd',
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 4,
      tickMarkFormatter: (time: Time, tickMarkType: TickMarkType) => {
        if (typeof time !== 'number') return ''
        const options: Intl.DateTimeFormatOptions =
          tickMarkType <= TickMarkType.DayOfMonth
            ? { month: '2-digit', day: '2-digit' }
            : { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
        return new Date(time * 1000).toLocaleString('zh-CN', {
          ...options,
          timeZone: 'Asia/Shanghai',
        })
      },
    },
    localization: {
      locale: 'zh-CN',
      // 轻量图表时间戳使用 UTC；展示明确采用活动的北京时间。
      timeFormatter: (time: number) =>
        new Date(time * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }),
    },
  })
  const candles = makeDemoCandles(props.outcome, props.interval)
  if (props.mode === 'candle') {
    const series = chart.addSeries(CandlestickSeries, {
      upColor: palette.up,
      downColor: palette.down,
      wickUpColor: palette.up,
      wickDownColor: palette.down,
      borderVisible: false,
      priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
    })
    series.setData(candles)
  } else {
    const series = chart.addSeries(AreaSeries, {
      lineColor: directionColor,
      topColor: withAlpha(directionColor, 35),
      bottomColor: withAlpha(directionColor, 0),
      lineWidth: 2,
      priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
    })
    series.setData(candles.map((candle) => ({ time: candle.time, value: candle.close })))
  }
  for (const [period, color, style] of [
    [5, '#111111', LineStyle.Solid],
    [20, '#888888', LineStyle.Dashed],
  ] as const) {
    const series = chart.addSeries(LineSeries, {
      color,
      lineStyle: style,
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    })
    series.setData(movingAverage(candles, period))
  }
  const volume = chart.addSeries(HistogramSeries, {
    priceFormat: { type: 'volume' },
    priceScaleId: '',
    priceLineVisible: false,
    lastValueVisible: false,
  })
  volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })
  volume.setData(
    candles.map((candle) => ({
      time: candle.time,
      value: candle.volume,
      color: withAlpha(candle.close >= candle.open ? palette.up : palette.down, 75),
    })),
  )
  chart.timeScale().fitContent()
}

function download() {
  if (!chart) return
  const anchor = document.createElement('a')
  anchor.download = `TouhouCCB-${props.outcome.code}-${props.interval}.png`
  anchor.href = chart.takeScreenshot().toDataURL('image/png')
  anchor.click()
}

watch(() => [props.outcome.id, props.interval, props.mode], draw)
onMounted(draw)
onBeforeUnmount(() => chart?.remove())
defineExpose({ download })
</script>

<template>
  <div ref="container" class="promo-chart" aria-label="模拟价格 K 线及成交量图表"></div>
</template>

<style scoped>
.promo-chart {
  width: 100%;
  height: 352px;
}
@media (max-width: 760px) {
  .promo-chart {
    height: 300px;
  }
}
</style>
