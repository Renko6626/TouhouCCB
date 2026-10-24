<script setup lang="ts">
import { computed, onMounted, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { fxApi, FxStream } from '@/api/fx'
import FxCandleChart from '@/components/chart/FxCandleChart.vue'
import type { FxChartInterval, FxPairPublic, FxPublicEnvelope } from '@/types/fx'

const route = useRoute()
const router = useRouter()
const pairs = ref<FxPairPublic[]>([])
const loading = ref(true)
const loadError = ref('')
const envelope = shallowRef<FxPublicEnvelope | null>(null)
const reloadToken = ref(0)
const controlsVisible = computed(() => route.query.controls !== '0')
const statusLabels: Record<FxPairPublic['status'], string> = {
  draft: '草稿', trading: '交易中', paused: '已暂停', closed: '已关闭',
}
const intervals: FxChartInterval[] = ['10s', '1m', '15m', '1h']
const intervalLabels: Record<FxChartInterval, string> = {
  '10s': '10 秒', '1m': '1 分钟', '15m': '15 分钟', '1h': '1 小时',
}
const interval = computed<FxChartInterval>(() => {
  const value = route.query.interval
  return intervals.includes(value as FxChartInterval) ? value as FxChartInterval : '1m'
})

const requestedPairId = computed(() => {
  const value = route.query.pair
  const id = typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : NaN
  return Number.isSafeInteger(id) && id > 0 ? id : null
})
const activePair = computed(() => {
  if (loading.value || loadError.value) return null
  if (route.query.pair !== undefined) {
    return pairs.value.find(pair => pair.id === requestedPairId.value) ?? null
  }
  return pairs.value.find(pair => pair.status === 'trading') ?? pairs.value[0] ?? null
})
const pairId = computed(() => activePair.value?.id ?? null)
const error = computed(() => {
  if (loading.value) return ''
  if (loadError.value) return loadError.value
  if (route.query.pair !== undefined && requestedPairId.value === null) {
    return '币对参数无效，请从工具栏重新选择币对。'
  }
  if (route.query.pair !== undefined && activePair.value === null) {
    return '该币对不存在或未公开，请从工具栏选择其他币对。'
  }
  return pairs.value.length === 0 ? '暂无可播放的 FX 币对。' : ''
})

async function loadPairs() {
  loading.value = true
  loadError.value = ''
  try {
    pairs.value = await fxApi.listPairs()
  } catch {
    loadError.value = '无法加载 FX 币对，请重试。'
  } finally {
    loading.value = false
  }
}
onMounted(loadPairs)

function updateQuery(values: Record<string, string | undefined>) {
  void router.replace({ query: { ...route.query, ...values }, hash: route.hash })
}
function selectPair(event: Event) {
  updateQuery({ pair: (event.target as HTMLSelectElement).value })
}
function selectInterval(event: Event) {
  updateQuery({ interval: (event.target as HTMLSelectElement).value })
}

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
    <header v-if="controlsVisible" class="fx-playback-toolbar" aria-label="播放设置">
      <span class="fx-playback-title">FX 行情</span>
      <label class="fx-playback-field fx-playback-pair">
        <span>币对</span>
        <select :value="pairId ?? ''" :disabled="loading || pairs.length === 0" @change="selectPair">
          <option v-if="pairId === null" value="" disabled>
            {{ loading ? '加载中…' : pairs.length ? '请选择币对' : '暂无币对' }}
          </option>
          <option v-for="pair in pairs" :key="pair.id" :value="pair.id">
            {{ pair.currency_name }}（{{ pair.currency_code }}） / 金圆券 — {{ statusLabels[pair.status] }}
          </option>
        </select>
      </label>
      <label class="fx-playback-field">
        <span>周期</span>
        <select :value="interval" @change="selectInterval">
          <option v-for="period in intervals" :key="period" :value="period">
            {{ intervalLabels[period] }}
          </option>
        </select>
      </label>
      <button class="fx-playback-hide" type="button" @click="updateQuery({ controls: '0' })">
        隐藏工具栏
      </button>
    </header>
    <button
      v-else
      class="fx-playback-restore"
      type="button"
      @click="updateQuery({ controls: undefined })"
    >
      显示工具栏
    </button>
    <div class="fx-playback-chart">
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
        <p>{{ error || '行情加载中…' }}</p>
        <button v-if="loadError" type="button" @click="loadPairs">重试</button>
        <button v-else-if="error && !controlsVisible" type="button" @click="updateQuery({ controls: undefined })">
          显示工具栏
        </button>
      </div>
    </div>
  </main>
</template>

<style scoped>
.fx-playback {
  position: fixed;
  inset: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: #fff;
}

.fx-playback-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px 20px;
  padding: 12px 16px;
  border-bottom: 1px solid #d4d4d4;
}

.fx-playback-title {
  font-size: 14px;
  font-weight: 700;
}

.fx-playback-field {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  font-size: 13px;
}

.fx-playback-field > span {
  flex-shrink: 0;
  color: #666;
}

select, button {
  min-height: 34px;
  border: 1px solid #b8b8b8;
  border-radius: 0;
  padding: 6px 10px;
  background: #fff;
  color: #222;
  font: inherit;
  font-size: 13px;
}

select { min-width: 0; max-width: 100%; }
button { cursor: pointer; }
button:hover { background: #f3f3f3; }
select:focus-visible, button:focus-visible { outline: 2px solid #222; outline-offset: 2px; }
.fx-playback-hide { margin-left: auto; }

.fx-playback-restore {
  position: absolute;
  top: 12px;
  left: 12px;
  z-index: 2;
  opacity: 0;
}

.fx-playback:hover > .fx-playback-restore,
.fx-playback-restore:focus-visible {
  opacity: 1;
}

.fx-playback-chart {
  flex: 1;
  min-height: 0;
  position: relative;
}

.fx-playback :deep(.fx-candle-chart) {
  min-height: 0;
}

.fx-playback-message {
  display: grid;
  align-content: center;
  justify-items: center;
  gap: 12px;
  box-sizing: border-box;
  height: 100%;
  place-items: center;
  padding: 24px;
  color: #666;
  text-align: center;
}

.fx-playback-message p { margin: 0; }

@media (max-width: 950px) {
  .fx-playback-toolbar { gap: 10px 12px; padding: 10px 12px; }
  .fx-playback-title { display: none; }
  .fx-playback-pair { flex-basis: 100%; }
  .fx-playback-pair select { flex: 1; }
}
</style>
