<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue'
import { useRoute } from 'vue-router'
import { fxApi, FxStream } from '@/api/fx'
import FxCandleChart from '@/components/chart/FxCandleChart.vue'
import type { FxChartInterval, FxPublicEnvelope } from '@/types/fx'

const route = useRoute()
const pairId = ref<number | null>(null)
const error = ref('')
const envelope = shallowRef<FxPublicEnvelope | null>(null)
const reloadToken = ref(0)
const intervals: FxChartInterval[] = ['10s', '1m', '15m', '1h']
const interval = computed<FxChartInterval>(() => {
  const value = route.query.interval
  return intervals.includes(value as FxChartInterval) ? value as FxChartInterval : '1m'
})

watch(
  () => route.query.pair,
  async (value, _previous, onCleanup) => {
    let active = true
    onCleanup(() => { active = false })
    pairId.value = null
    error.value = ''

    if (value !== undefined) {
      const id = typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : NaN
      if (!Number.isSafeInteger(id) || id <= 0) {
        error.value = '币对参数无效，请在网址中使用 pair=币对ID。'
        return
      }
      pairId.value = id
      return
    }

    try {
      const pairs = await fxApi.listPairs()
      if (!active) return
      pairId.value = pairs[0]?.id ?? null
      if (!pairId.value) error.value = '暂无可播放的 FX 币对。'
    } catch {
      if (active) error.value = '无法加载 FX 币对，请刷新页面重试。'
    }
  },
  { immediate: true },
)

watch(pairId, (id, _previous, onCleanup) => {
  envelope.value = null
  reloadToken.value = 0
  if (id === null) return

  const stream = new FxStream()
  let interrupted = false
  stream.onEnvelope((value) => { envelope.value = value })
  stream.onError(() => { interrupted = true })
  stream.onOpen(() => {
    if (interrupted) reloadToken.value += 1
    interrupted = false
  })
  stream.connect(id)
  onCleanup(() => stream.disconnect())
}, { immediate: true, flush: 'sync' })
</script>

<template>
  <main class="fx-playback" aria-label="FX K 线行情播放">
    <FxCandleChart
      v-if="pairId !== null"
      :key="pairId"
      :pair-id="pairId"
      :interval="interval"
      :envelope="envelope"
      :reload-token="reloadToken"
      height="100%"
    />
    <div v-else class="fx-playback-message" role="status">
      {{ error || '行情加载中…' }}
    </div>
  </main>
</template>

<style scoped>
.fx-playback {
  position: fixed;
  inset: 0;
  overflow: hidden;
  background: #fff;
}

.fx-playback :deep(.fx-candle-chart) {
  min-height: 0;
}

.fx-playback-message {
  display: grid;
  height: 100%;
  place-items: center;
  padding: 24px;
  color: #666;
  text-align: center;
}
</style>
