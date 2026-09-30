<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { fxApi, formatFxPrice } from '@/api/fx'
import type { FxPairPublic, FxSnapshot } from '@/types/fx'
import { useAuthStore } from '@/stores/auth'
import { useUserStore } from '@/stores/user'
import { FX_DAY_MS, fxOverviewTrend } from '@/utils/fxOverview'

const authStore = useAuthStore()
const userStore = useUserStore()
const pairs = ref<FxPairPublic[]>([])
const limit = ref(6)
const visiblePairs = computed(() => pairs.value.slice(0, limit.value))
interface Card { snapshot?: FxSnapshot; trend?: ReturnType<typeof fxOverviewTrend>; chartError?: boolean; updatedAt: string }
const cards = ref<Record<number, Card>>({})
const loading = ref(false)
const error = ref(false)
let generation = 0
const heldIds = computed(() => new Set(authStore.isAuthenticated
  ? (userStore.summary?.fx_wallets ?? []).filter(wallet => wallet.foreign_amount > 0).map(wallet => wallet.pair_id) : []))
const statusNames: Record<string, string> = { trading: '交易中', paused: '暂停', closed: '已闭市' }
const changeText = (change: number) => `${change > 0 ? '+' : ''}${change.toFixed(2)}%`
const direction = (change: number | null | undefined) => change == null || change === 0 ? 'flat' : change > 0 ? 'up' : 'down'

async function loadCards(target: FxPairPublic[], request: number) {
  const now = Date.now()
  // One extra hour includes a fully closed 15m bucket preceding the 24h boundary.
  const from = new Date(now - FX_DAY_MS - 60 * 60 * 1000).toISOString()
  const to = new Date(now).toISOString()
  await Promise.all(target.map(async pair => {
    const [snapshot, chart] = await Promise.allSettled([
      fxApi.getSnapshot(pair.id), fxApi.getChart(pair.id, '15m', from, to),
    ])
    if (request !== generation) return
    const data = snapshot.status === 'fulfilled' ? snapshot.value : undefined
    cards.value[pair.id] = {
      snapshot: data,
      trend: chart.status === 'fulfilled' ? fxOverviewTrend(chart.value, data?.price, now) : undefined,
      chartError: chart.status === 'rejected',
      updatedAt: new Date().toLocaleTimeString(),
    }
  }))
}
async function refresh() {
  if (loading.value) return
  const request = ++generation
  loading.value = true
  error.value = false
  try {
    const list = await fxApi.listPairs()
    if (request !== generation) return
    pairs.value = [...list].sort((a, b) => Number(b.status === 'trading') - Number(a.status === 'trading') || a.currency_code.localeCompare(b.currency_code))
    cards.value = {}
    await loadCards(visiblePairs.value, request)
  } catch {
    if (request === generation) error.value = true
  } finally {
    if (request === generation) loading.value = false
  }
}
async function showMore() {
  if (loading.value) return
  const previous = limit.value
  limit.value += 6
  const request = ++generation
  loading.value = true
  try { await loadCards(pairs.value.slice(previous, limit.value), request) }
  finally { if (request === generation) loading.value = false }
}
onMounted(refresh)
onBeforeUnmount(() => { generation++ })
</script>

<template>
  <section class="fx-overview" aria-labelledby="fx-overview-title">
    <header>
      <div><h2 id="fx-overview-title">外汇行情</h2><p>金圆券 / 1 外币 · 近 24h 走势（15 分钟采样）</p></div>
      <button :disabled="loading" @click="refresh">{{ loading ? '刷新中…' : '刷新行情' }}</button>
    </header>
    <p v-if="error" class="state" role="alert">行情列表刷新失败，请重试。已有卡片为上次快照。</p>
    <p v-if="loading && !pairs.length" class="state">正在读取外汇行情…</p>
    <p v-else-if="!pairs.length && !error" class="state">暂无公开外汇品种，开市后会在这里展示。</p>
    <div v-if="pairs.length" class="fx-pairs">
      <router-link v-for="pair in visiblePairs" :key="pair.id" :to="{ path: '/fx', query: { pair: pair.id } }" class="fx-pair">
        <div class="pair-heading"><h3>{{ pair.currency_name }} <small>{{ pair.currency_code }}</small></h3><span>{{ statusNames[pair.status] ?? pair.status }} <b v-if="heldIds.has(pair.id)" class="held">持有</b></span></div>
        <template v-if="cards[pair.id]?.snapshot">
          <div class="pair-price">{{ formatFxPrice(cards[pair.id]!.snapshot!.price) }}</div>
          <p class="change" :class="direction(cards[pair.id]?.trend?.change)">
            <template v-if="cards[pair.id]?.trend?.change != null">≈ {{ changeText(cards[pair.id]!.trend!.change!) }} <span>24h</span></template>
            <template v-else>{{ cards[pair.id]?.chartError ? '走势暂不可用' : '暂无24h基准' }}</template>
          </p>
          <svg v-if="cards[pair.id]?.trend?.path" class="sparkline" :class="direction(cards[pair.id]?.trend?.change)" viewBox="0 0 240 48" role="img" :aria-label="`${pair.currency_name}近24小时价格走势，15分钟采样`"><polyline :points="cards[pair.id]!.trend!.path" fill="none" stroke="currentColor" stroke-width="2" vector-effect="non-scaling-stroke" /></svg>
          <p v-else class="chart-empty">{{ cards[pair.id]?.chartError ? '图表加载失败，可刷新重试' : '近24h走势数据不足' }}</p>
          <dl><div><dt>买入价</dt><dd>{{ formatFxPrice(cards[pair.id]!.snapshot!.buy_price) }}</dd></div><div><dt>卖出价</dt><dd>{{ formatFxPrice(cards[pair.id]!.snapshot!.sell_price) }}</dd></div></dl>
        </template>
        <p v-else class="state">{{ cards[pair.id] ? '该币种报价暂不可用，请刷新重试' : '正在读取报价与走势…' }}</p>
        <div class="card-footer"><span class="pair-link">查看 K 线与交易 →</span><small v-if="cards[pair.id]">{{ cards[pair.id]!.updatedAt }} 读取</small></div>
      </router-link>
    </div>
    <footer><p>24h 涨跌以窗口前已收盘的 15 分钟 K 线近似计算；缺少基准时不显示涨跌幅。</p><button v-if="limit < pairs.length" :disabled="loading" @click="showMore">再显示 {{ Math.min(6, pairs.length - limit) }} 个（已显示 {{ Math.min(limit, pairs.length) }} / {{ pairs.length }}）</button></footer>
  </section>
</template>

<style scoped>
header, .pair-heading { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
header { border-bottom: 2px solid #000; padding-bottom: 12px; margin-bottom: 16px; }
h2 { font-size: 20px; font-weight: 800; margin: 0; }
header p { margin: 4px 0 0; color: #666; font-size: 12px; }
button { border: 2px solid #000; background: #fff; padding: 6px 12px; cursor: pointer; }
button:disabled { opacity: .5; cursor: wait; }
.fx-pairs { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr)); gap: 16px; }
.fx-pair { display: block; border: 2px solid #000; padding: 20px; color: #000; text-decoration: none; background: #fff; }
.fx-pair:hover { background: #f5f5f5; }
.fx-pair:focus-visible, button:focus-visible { outline: 3px solid #555; outline-offset: 3px; }
h3 { margin: 0; font-size: 18px; font-weight: 800; overflow-wrap: anywhere; }
small, .pair-heading > span { font-size: 12px; font-weight: 400; color: #555; }
.pair-price { font-size: clamp(26px, 4vw, 36px); font-weight: 800; font-variant-numeric: tabular-nums; margin: 16px 0; overflow-wrap: anywhere; }
dl { display: flex; flex-wrap: wrap; gap: 12px 32px; margin-bottom: 16px; font-size: 12px; }
dt { color: #666; } dd { margin: 4px 0 0; font-weight: 600; }
.pair-link { font-size: 12px; text-decoration: underline; text-underline-offset: 3px; }
.state { padding: 20px 0; color: #666; font-size: 14px; }
.sparkline { width: 100%; height: 48px; display: block; }
.change { margin: -8px 0 8px; font-size: 14px; font-weight: 700; }
.change span { font-size: 11px; color: #666; }
.up { color: var(--color-up); } .down { color: var(--color-down); } .flat { color: #666; }
.chart-empty { height: 48px; display: flex; align-items: center; margin: 0; color: #777; font-size: 12px; }
.held { border: 1px solid #000; padding: 2px 4px; margin-left: 6px; color: #000; }
.card-footer { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; }
.card-footer small, footer p { color: #777; font-size: 11px; }
footer { margin-top: 12px; }
</style>
