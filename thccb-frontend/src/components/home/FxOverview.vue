<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { fxApi, formatFxPrice } from '@/api/fx'
import type { FxHistorySnapshotTail, FxPairPublic, FxSnapshot } from '@/types/fx'
import { useAuthStore } from '@/stores/auth'
import { useUserStore } from '@/stores/user'
import { loadFxHistoryResult } from '@/composables/useFxCandleHistory'
import { fxOverviewTrend } from '@/utils/fxOverview'

/** 主页 HTTP 快照新增的可选公开历史元数据；用局部结构交叉消费，不改 F 拥有的类型文件。 */
type FxSnapshotHistory = FxSnapshot & { history_version?: string | null; history_ready?: boolean }

/** 25h 覆盖完整 24h 窗口，并留出窗口前已收盘的 15m 基准桶。 */
const FX_OVERVIEW_LOOKBACK_MINUTES = 25 * 60

const authStore = useAuthStore()
const userStore = useUserStore()
const pairs = ref<FxPairPublic[]>([])
const limit = ref(6)
const visiblePairs = computed(() => pairs.value.slice(0, limit.value))
interface Card {
  snapshot?: FxSnapshot
  trend?: ReturnType<typeof fxOverviewTrend>
  /** 本次刷新报价失败，snapshot 仍为上次成功值 */
  quoteError?: boolean
  /** 本次刷新走势失败，trend 仍为上次成功值 */
  trendError?: boolean
  updatedAt: string
}
const cards = ref<Record<number, Card>>({})
const loading = ref(false)
const error = ref(false)
let generation = 0
const heldIds = computed(() => new Set(authStore.isAuthenticated
  ? (userStore.summary?.fx_wallets ?? []).filter(wallet => wallet.foreign_amount > 0).map(wallet => wallet.pair_id) : []))
const statusNames: Record<string, string> = { trading: '交易中', paused: '暂停', closed: '已闭市' }
const changeText = (change: number) => `${change > 0 ? '+' : ''}${change.toFixed(2)}%`
const direction = (change: number | null | undefined) => change == null || change === 0 ? 'flat' : change > 0 ? 'up' : 'down'

/** 用 HTTP 快照的可选元数据构造历史上下文；未就绪/无版本时适配层自动回退带游标的 /chart。 */
function snapshotTailFor(snapshot: FxSnapshot): FxHistorySnapshotTail {
  const meta = snapshot as FxSnapshotHistory
  return {
    history_version: meta.history_version ?? null,
    history_tail: null,
    history_tail_at: null,
    history_tail_through_trade_id: null,
    history_ready: meta.history_ready === true,
  }
}

function updateCard(pairId: number, patch: Partial<Card>) {
  const existing = cards.value[pairId] ?? { updatedAt: '' }
  cards.value[pairId] = { ...existing, ...patch }
}

/** 报价先返回先展示；失败保留上次成功值并标记 stale。 */
async function loadQuote(pair: FxPairPublic, request: number): Promise<FxSnapshot | undefined> {
  try {
    const snapshot = await fxApi.getSnapshot(pair.id)
    if (request !== generation) return undefined
    updateCard(pair.id, { snapshot, quoteError: false, updatedAt: new Date().toLocaleTimeString() })
    return snapshot
  } catch {
    if (request === generation) updateCard(pair.id, { quoteError: true, updatedAt: new Date().toLocaleTimeString() })
    return undefined
  }
}

/** 走势独立加载：只有本次拿到成功报价才计算 24h 涨跌，绝不用过期价格伪造新的涨跌。 */
async function loadTrend(pair: FxPairPublic, snapshot: FxSnapshot | undefined, request: number) {
  if (!snapshot) return
  try {
    const result = await loadFxHistoryResult(pair.id, '15m', FX_OVERVIEW_LOOKBACK_MINUTES, snapshotTailFor(snapshot))
    if (request !== generation) return
    if (cards.value[pair.id]?.snapshot !== snapshot) return
    updateCard(pair.id, { trend: fxOverviewTrend(result.points, snapshot.price, Date.now()), trendError: false })
  } catch {
    if (request !== generation) return
    if (!cards.value[pair.id]) return
    updateCard(pair.id, { trendError: true })
  }
}

async function loadCards(target: FxPairPublic[], request: number) {
  await Promise.all(target.map(async pair => {
    const snapshot = await loadQuote(pair, request)
    if (request !== generation) return
    await loadTrend(pair, snapshot, request)
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
    // 保留已有卡片：单项失败时继续显示上次成功快照/走势，不因整体刷新清空。
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
          <p v-if="cards[pair.id]?.quoteError" class="stale-hint" role="status">报价刷新失败，显示上次快照</p>
          <p v-else-if="cards[pair.id]?.trendError && cards[pair.id]?.trend" class="stale-hint" role="status">走势刷新失败，显示上次走势</p>
          <p class="change" :class="direction(cards[pair.id]?.trend?.change)">
            <template v-if="cards[pair.id]?.trend?.change != null">≈ {{ changeText(cards[pair.id]!.trend!.change!) }} <span>24h</span></template>
            <template v-else>{{ cards[pair.id]?.trendError ? '走势暂不可用' : '暂无24h基准' }}</template>
          </p>
          <svg v-if="cards[pair.id]?.trend?.path" class="sparkline" :class="direction(cards[pair.id]?.trend?.change)" viewBox="0 0 240 48" role="img" :aria-label="`${pair.currency_name}近24小时价格走势，15分钟采样`"><polyline :points="cards[pair.id]!.trend!.path" fill="none" stroke="currentColor" stroke-width="2" vector-effect="non-scaling-stroke" /></svg>
          <p v-else class="chart-empty">{{ cards[pair.id]?.trendError ? '图表加载失败，可刷新重试' : '近24h走势数据不足' }}</p>
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
.stale-hint { margin: -8px 0 8px; color: #b45309; font-size: 11px; font-weight: 600; }
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
