<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { chartApi } from '@/api/chart'

/**
 * 首页「我的持仓」卡片带（同花顺自选股式）。
 * 纯前端派生：summary.positions × priceContext，不新增接口。
 * 主指标涨跌（市场维度，1h / 24h 可切）、副指标较成本（个人维度）；按市值倒序取前 N。
 *  - 24h 基准：/market/list 自带 price_change_24h 反推（priceContext.prices24hAgo）
 *  - 1h 基准：/chart/candles 1m 窗口首根 K 线的 open（= 该笔成交前价 = 窗口起点价）；
 *    窗口内无 K 线 = 这一小时没成交 = 基准即当前价（0%）
 */
const props = withDefaults(defineProps<{ limit?: number }>(), { limit: 5 })

const router = useRouter()
const userStore = useUserStore()

type Win = '1h' | '24h'
const WIN_KEY = 'home.holdingsStrip.window'
const win = ref<Win>('1h')
try {
  const saved = localStorage.getItem(WIN_KEY)
  if (saved === '1h' || saved === '24h') win.value = saved
} catch { /* 存储不可用时用默认 */ }
const setWin = (w: Win) => {
  win.value = w
  try { localStorage.setItem(WIN_KEY, w) } catch { /* ignore */ }
}

/** outcome_id → 1h 前价格；undefined = 未拉取/失败，null = 窗口内无成交（用当前价） */
const prices1hAgo = ref<Map<number, number | null>>(new Map())

interface Row {
  key: string
  marketId: number
  title: string
  label: string
  price: number
  /** 24h 涨跌百分比；无基准为 null */
  change24hPct: number | null
  amount: number
  marketValue: number
  /** 较成本百分比；成本为 0 时 null */
  vsCostPct: number | null
  pnl: number
  tradable: boolean
}

const rows = computed<Row[]>(() => {
  const positions = userStore.summary?.positions ?? []
  const ctxMap = userStore.priceContext
  const out: Row[] = []
  for (const p of positions) {
    if (p.amount <= 0) continue
    const ctx = ctxMap.get(p.market_id)
    if (!ctx) continue
    const idx = ctx.outcomeIds.indexOf(p.outcome_id)
    if (idx < 0) continue
    const price = ctx.prices[idx] ?? 0
    let prev: number | null
    if (win.value === '24h') {
      prev = ctx.prices24hAgo[idx] ?? null
    } else {
      const h = prices1hAgo.value.get(p.outcome_id)
      prev = h === undefined ? null : h === null ? price : h
    }
    const marketValue = p.amount * price
    out.push({
      key: `${p.market_id}-${p.outcome_id}`,
      marketId: p.market_id,
      title: ctx.title,
      label: ctx.outcomeLabels[idx] ?? '',
      price,
      change24hPct: prev != null && prev > 0 ? ((price - prev) / prev) * 100 : null,
      amount: p.amount,
      marketValue,
      vsCostPct: p.cost_basis > 0 ? ((marketValue - p.cost_basis) / p.cost_basis) * 100 : null,
      pnl: marketValue - p.cost_basis,
      tradable: String(ctx.status).toLowerCase() === 'trading',
    })
  }
  out.sort((a, b) => b.marketValue - a.marketValue)
  return out.slice(0, props.limit)
})

/** 前 N 持仓的 outcome id 集合变化时拉一次 1h 基准（与窗口选择无关，切换即时生效） */
const topOutcomeIds = computed(() => {
  const positions = [...(userStore.summary?.positions ?? [])]
    .filter(p => p.amount > 0)
    .map(p => {
      const ctx = userStore.priceContext.get(p.market_id)
      const idx = ctx ? ctx.outcomeIds.indexOf(p.outcome_id) : -1
      return { id: p.outcome_id, mv: idx >= 0 ? p.amount * (ctx!.prices[idx] ?? 0) : 0 }
    })
    .sort((a, b) => b.mv - a.mv)
    .slice(0, props.limit)
  return positions.map(p => p.id)
})

const fetch1hBaseline = async (ids: number[]) => {
  const to = new Date()
  const from = new Date(to.getTime() - 3600_000)
  const results = await Promise.all(ids.map(async id => {
    try {
      const res = await chartApi.getCandles(id, '1m', from.toISOString(), to.toISOString(), false, 120)
      const first = res.candles[0]
      return [id, first ? first.o : null] as const
    } catch (e) {
      console.warn('[holdings-strip] 1h 基准拉取失败', id, e)
      return [id, undefined] as const
    }
  }))
  const next = new Map(prices1hAgo.value)
  for (const [id, v] of results) {
    if (v === undefined) next.delete(id)
    else next.set(id, v)
  }
  prices1hAgo.value = next
}

watch(topOutcomeIds, (ids, old) => {
  if (!ids.length) return
  if (old && ids.length === old.length && ids.every((v, i) => v === old[i])) return
  void fetch1hBaseline(ids)
}, { immediate: true })

const dir = (v: number | null): 'up' | 'down' | 'flat' =>
  v == null || Math.abs(v) < 0.005 ? 'flat' : v > 0 ? 'up' : 'down'

const fmtPct = (v: number | null) =>
  v == null ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(1)}%`

const fmtAmount = (v: number) =>
  v >= 1000 ? Math.round(v).toLocaleString() : v.toFixed(v >= 100 ? 0 : 1)

const goTrade = (marketId: number) => router.push(`/market/${marketId}/trade`)
</script>

<template>
  <div v-if="rows.length" class="holdings-strip">
    <div class="strip-header">
      <span class="strip-title">我的持仓 <span class="strip-sub">· 按市值</span></span>
      <div class="strip-right">
        <div class="win-toggle" role="tablist">
          <button
            v-for="w in (['1h', '24h'] as Win[])"
            :key="w"
            class="win-btn"
            :class="{ 'win-btn-active': win === w }"
            role="tab"
            :aria-selected="win === w"
            @click="setWin(w)"
          >{{ w }}</button>
        </div>
        <button class="strip-link" @click="router.push('/user/portfolio')">查看全部 →</button>
      </div>
    </div>
    <div class="strip-scroller">
      <button
        v-for="r in rows"
        :key="r.key"
        class="h-card"
        :class="[`h-card-${dir(r.change24hPct)}`, { 'h-card-paused': !r.tradable }]"
        :title="`${r.title} / ${r.label}`"
        @click="goTrade(r.marketId)"
      >
        <div class="h-title">{{ r.title }}</div>
        <div class="h-label">
          {{ r.label }}
          <span v-if="!r.tradable" class="h-paused-tag">暂停</span>
        </div>
        <div class="h-main">
          <span class="h-price">{{ r.price.toFixed(4) }}</span>
          <span class="h-change" :class="`c-${dir(r.change24hPct)}`">
            <span class="h-arrow">{{ dir(r.change24hPct) === 'up' ? '▲' : dir(r.change24hPct) === 'down' ? '▼' : '' }}</span>{{ fmtPct(r.change24hPct) }}
          </span>
        </div>
        <div class="h-sub">
          <span>持 {{ fmtAmount(r.amount) }}</span>
          <span class="h-sep">·</span>
          <span>较成本 <b :class="`c-${dir(r.vsCostPct)}`">{{ fmtPct(r.vsCostPct) }}</b></span>
        </div>
      </button>
    </div>
  </div>
</template>

<style scoped>
.holdings-strip {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 22px;
  margin-bottom: 26px;
}

.strip-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  border-bottom: 2px solid #000000;
  padding-bottom: 6px;
}

.strip-title {
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: #000000;
}

.strip-sub {
  font-weight: 500;
  letter-spacing: 0;
  text-transform: none;
  color: var(--color-text-muted);
}

.strip-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.win-toggle {
  display: inline-flex;
  border: 2px solid #000000;
}

.win-btn {
  padding: 1px 8px;
  font: inherit;
  font-size: 11px;
  font-weight: 700;
  background: #ffffff;
  color: #000000;
  border: none;
  cursor: pointer;
}

.win-btn + .win-btn { border-left: 2px solid #000000; }
.win-btn-active { background: #000000; color: #ffffff; }

.strip-link {
  background: none;
  border: none;
  padding: 0;
  font-size: 12px;
  font-weight: 600;
  color: #000000;
  cursor: pointer;
  text-decoration: underline;
  text-underline-offset: 3px;
}

.strip-scroller {
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: minmax(0, 1fr);
  gap: 10px;
}

.h-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  padding: 10px 12px;
  background: #ffffff;
  border: 2px solid #000000;
  text-align: left;
  cursor: pointer;
  font: inherit;
  color: #000000;
  transition: transform 0.08s ease, box-shadow 0.08s ease;
}

.h-card:hover {
  transform: translate(-2px, -2px);
  box-shadow: 4px 4px 0 #000000;
}

.h-card::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 4px;
  background: #000000;
}

.h-card-up::before { background: var(--color-up); }
.h-card-down::before { background: var(--color-down); }
.h-card-paused { opacity: 0.6; }

.h-title {
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--color-text-muted);
}

.h-label {
  font-size: 14px;
  font-weight: 700;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.h-paused-tag {
  display: inline-block;
  margin-left: 6px;
  padding: 0 4px;
  font-size: 10px;
  font-weight: 700;
  border: 1px solid #000000;
  vertical-align: 2px;
}

.h-main {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-top: 2px;
  font-variant-numeric: tabular-nums;
}

.h-price {
  font-size: 18px;
  font-weight: 700;
}

.h-change {
  font-size: 14px;
  font-weight: 700;
  white-space: nowrap;
}

.h-arrow { font-size: 10px; margin-right: 2px; }

.h-sub {
  font-size: 11px;
  color: var(--color-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-variant-numeric: tabular-nums;
}

.h-sep { margin: 0 4px; }

.c-up { color: var(--color-up); }
.c-down { color: var(--color-down); }
.c-flat { color: var(--color-text-muted); }

@media (max-width: 768px) {
  .strip-scroller {
    grid-auto-columns: 56vw;
    overflow-x: auto;
    scroll-snap-type: x mandatory;
    padding-bottom: 4px;
    margin-right: -16px;
    padding-right: 16px;
    -webkit-overflow-scrolling: touch;
  }
  .h-card { scroll-snap-align: start; }
}
</style>
