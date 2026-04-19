<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { marketApi } from '@/api/market'
import type { RecentTrade } from '@/types/api'

const router = useRouter()

const trades = ref<RecentTrade[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
let pollTimer: ReturnType<typeof setInterval> | null = null

const POLL_INTERVAL_MS = 5000
const FETCH_LIMIT = 30

const fetchTrades = async () => {
  try {
    error.value = null
    const data = await marketApi.getRecentTrades(FETCH_LIMIT)
    trades.value = data
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loading.value = true
  fetchTrades()
  pollTimer = setInterval(fetchTrades, POLL_INTERVAL_MS)
})

onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
})

// 相对时间："3秒前" / "2分钟前" / "1小时前" / 否则绝对时间 HH:MM:SS
const relativeTime = (iso: string) => {
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const diffSec = Math.max(0, Math.floor((Date.now() - t) / 1000))
  if (diffSec < 60) return `${diffSec}秒前`
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}分钟前`
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}小时前`
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

const truncate = (s: string, n: number) => s.length > n ? s.slice(0, n - 1) + '…' : s

const goToMarket = (id: number) => router.push(`/market/${id}/trade`)
</script>

<template>
  <section class="recent-trades">
    <header class="rt-head">
      <h2 class="rt-title">实时成交</h2>
      <span class="rt-tip">每 5 秒刷新</span>
    </header>

    <div v-if="loading && !trades.length" class="rt-state">加载中…</div>
    <div v-else-if="error" class="rt-state rt-error">{{ error }}</div>
    <div v-else-if="!trades.length" class="rt-state">暂无近期成交</div>

    <ul v-else class="rt-list">
      <TransitionGroup name="rt-flip">
        <li
          v-for="t in trades"
          :key="t.id"
          class="rt-row"
          :class="t.type === 'buy' ? 'rt-row-buy' : 'rt-row-sell'"
        >
          <span class="rt-time">{{ relativeTime(t.timestamp) }}</span>
          <span class="rt-user" :title="t.username">{{ truncate(t.username, 12) }}</span>
          <span class="rt-action">
            <span :class="['rt-tag', t.type === 'buy' ? 'rt-tag-buy' : 'rt-tag-sell']">
              {{ t.type === 'buy' ? '买入' : '卖出' }}
            </span>
          </span>
          <span class="rt-shares">{{ Number(t.shares).toLocaleString() }}</span>
          <span class="rt-outcome">{{ truncate(t.outcome_label, 8) }}</span>
          <span
            class="rt-market"
            role="button"
            tabindex="0"
            :title="t.market_title"
            @click="goToMarket(t.market_id)"
            @keyup.enter="goToMarket(t.market_id)"
          >
            {{ truncate(t.market_title, 18) }}
          </span>
          <span class="rt-price">¥{{ Number(t.price).toFixed(4) }}</span>
        </li>
      </TransitionGroup>
    </ul>
  </section>
</template>

<style scoped>
.recent-trades {
  border: 2px solid #000;
  background: #fff;
  box-shadow: 4px 4px 0 #000;
}

.rt-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: #000;
  color: #fff;
  border-bottom: 2px solid #000;
}

.rt-title {
  font-size: 14px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin: 0;
}

.rt-tip {
  font-size: 11px;
  color: rgba(255, 255, 255, 0.6);
  letter-spacing: 0.04em;
}

.rt-state {
  padding: 32px 16px;
  text-align: center;
  font-size: 12px;
  color: #888;
}

.rt-error { color: var(--color-down); }

.rt-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 480px;
  overflow-y: auto;
}

.rt-row {
  display: grid;
  grid-template-columns: 64px 84px 52px 60px 80px 1fr auto;
  gap: 10px;
  align-items: center;
  padding: 8px 14px;
  border-bottom: 1px solid #f0f0f0;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.rt-row:last-child { border-bottom: none; }

.rt-row-buy { border-left: 3px solid var(--color-up); }
.rt-row-sell { border-left: 3px solid var(--color-down); }

.rt-time {
  font-size: 11px;
  color: #888;
  white-space: nowrap;
}

.rt-user {
  font-weight: 600;
  color: #000;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rt-action {
  display: flex;
  align-items: center;
}

.rt-tag {
  display: inline-block;
  padding: 1px 6px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  border: 1px solid #000;
}

.rt-tag-buy {
  background: var(--color-up);
  color: #fff;
  border-color: var(--color-up);
}

.rt-tag-sell {
  background: var(--color-down);
  color: #fff;
  border-color: var(--color-down);
}

.rt-shares {
  font-weight: 600;
  color: #000;
  text-align: right;
}

.rt-outcome {
  color: #444;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rt-market {
  color: #555;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
  text-decoration: underline;
  text-decoration-color: transparent;
  transition: text-decoration-color 0.1s, color 0.1s;
}

.rt-market:hover,
.rt-market:focus-visible {
  color: #000;
  text-decoration-color: #000;
  outline: none;
}

.rt-price {
  font-weight: 700;
  color: #000;
  white-space: nowrap;
}

/* 滚入动画 */
.rt-flip-enter-active {
  transition: transform 0.25s ease, opacity 0.25s ease;
}
.rt-flip-leave-active {
  transition: opacity 0.15s ease;
  position: absolute;
}
.rt-flip-enter-from {
  opacity: 0;
  transform: translateY(-12px);
}
.rt-flip-leave-to {
  opacity: 0;
}
.rt-flip-move {
  transition: transform 0.25s ease;
}

@media (max-width: 768px) {
  .rt-row {
    grid-template-columns: 56px 64px 44px 52px 1fr auto;
    gap: 8px;
    font-size: 11px;
  }
  /* 手机端隐藏 outcome 单独列，合到 market 后面 */
  .rt-outcome { display: none; }
}
</style>
