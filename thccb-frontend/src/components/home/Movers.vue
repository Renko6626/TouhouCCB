<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRouter } from 'vue-router'
import { marketApi } from '@/api/market'
import type { Mover, MoverWindow } from '@/types/api'

const router = useRouter()

const window = ref<MoverWindow>('24h')
const movers = ref<Mover[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
let pollTimer: ReturnType<typeof setInterval> | null = null

const WINDOWS: { value: MoverWindow; label: string }[] = [
  { value: '10min', label: '10 分钟' },
  { value: '1h', label: '1 小时' },
  { value: '24h', label: '24 小时' },
]

const TOP_N = 5

const gainers = computed(() =>
  movers.value.filter(m => m.change_pct > 0).slice(0, TOP_N)
)
const losers = computed(() =>
  movers.value.filter(m => m.change_pct < 0).slice(0, TOP_N)
)

const fetchMovers = async () => {
  try {
    error.value = null
    const data = await marketApi.getMovers(window.value, 30)
    movers.value = data
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

watch(window, () => {
  loading.value = true
  fetchMovers()
})

onMounted(() => {
  loading.value = true
  fetchMovers()
  pollTimer = setInterval(fetchMovers, 30000)
})

onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
})

const goToMarket = (id: number) => router.push(`/market/${id}/trade`)

// 截断显示
const truncate = (s: string, n: number) => s.length > n ? s.slice(0, n - 1) + '…' : s
</script>

<template>
  <section class="movers">
    <header class="movers-head">
      <h2 class="movers-title">涨跌榜</h2>
      <div class="movers-tabs" role="tablist">
        <button
          v-for="w in WINDOWS"
          :key="w.value"
          :class="['tab', { 'tab-active': window === w.value }]"
          @click="window = w.value"
          role="tab"
          :aria-selected="window === w.value"
        >
          {{ w.label }}
        </button>
      </div>
    </header>

    <div v-if="loading && !movers.length" class="movers-state">加载中…</div>
    <div v-else-if="error" class="movers-state movers-error">{{ error }}</div>
    <div v-else-if="!gainers.length && !losers.length" class="movers-state">
      该时间段内暂无显著价格变动
    </div>

    <div v-else class="movers-grid">
      <!-- 涨幅 -->
      <div class="movers-col">
        <div class="col-head col-head-up">涨幅 Top {{ gainers.length }}</div>
        <ol v-if="gainers.length" class="movers-list">
          <li
            v-for="(m, i) in gainers"
            :key="`up-${m.outcome_id}`"
            class="movers-row"
            @click="goToMarket(m.market_id)"
            role="button"
            tabindex="0"
            @keyup.enter="goToMarket(m.market_id)"
          >
            <span class="row-rank">{{ i + 1 }}</span>
            <span class="row-main">
              <span class="row-outcome">{{ truncate(m.outcome_label, 10) }}</span>
              <span class="row-market">{{ truncate(m.market_title, 20) }}</span>
            </span>
            <span class="row-change row-change-up">+{{ m.change_pct.toFixed(2) }}%</span>
          </li>
        </ol>
        <div v-else class="col-empty">暂无</div>
      </div>

      <!-- 跌幅 -->
      <div class="movers-col">
        <div class="col-head col-head-down">跌幅 Top {{ losers.length }}</div>
        <ol v-if="losers.length" class="movers-list">
          <li
            v-for="(m, i) in losers"
            :key="`down-${m.outcome_id}`"
            class="movers-row"
            @click="goToMarket(m.market_id)"
            role="button"
            tabindex="0"
            @keyup.enter="goToMarket(m.market_id)"
          >
            <span class="row-rank">{{ i + 1 }}</span>
            <span class="row-main">
              <span class="row-outcome">{{ truncate(m.outcome_label, 10) }}</span>
              <span class="row-market">{{ truncate(m.market_title, 20) }}</span>
            </span>
            <span class="row-change row-change-down">{{ m.change_pct.toFixed(2) }}%</span>
          </li>
        </ol>
        <div v-else class="col-empty">暂无</div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.movers {
  border: 2px solid #000;
  background: #fff;
  box-shadow: 4px 4px 0 #000;
}

.movers-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  background: #000;
  color: #fff;
  border-bottom: 2px solid #000;
}

.movers-title {
  font-size: 14px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin: 0;
}

.movers-tabs {
  display: flex;
  gap: 0;
}

.tab {
  padding: 4px 10px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  background: transparent;
  color: rgba(255, 255, 255, 0.6);
  border: 1px solid rgba(255, 255, 255, 0.3);
  cursor: pointer;
  transition: background 0.12s, color 0.12s, border-color 0.12s;
}

.tab + .tab { border-left: none; }

.tab:hover {
  color: #fff;
  background: rgba(255, 255, 255, 0.1);
}

.tab-active {
  background: #fff;
  color: #000;
  border-color: #fff;
}

.movers-state {
  padding: 32px 16px;
  text-align: center;
  font-size: 12px;
  color: #888;
}

.movers-error {
  color: var(--color-down);
}

.movers-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0;
}

.movers-col {
  border-right: 1px solid #e0e0e0;
}

.movers-col:last-child {
  border-right: none;
}

.col-head {
  padding: 8px 14px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  background: #f5f5f5;
  border-bottom: 1px solid #e0e0e0;
}

.col-head-up { color: var(--color-up); }
.col-head-down { color: var(--color-down); }

.movers-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.movers-row {
  display: grid;
  grid-template-columns: 24px 1fr auto;
  gap: 8px;
  align-items: center;
  padding: 10px 14px;
  border-bottom: 1px solid #f0f0f0;
  cursor: pointer;
  transition: background 0.1s;
}

.movers-row:last-child { border-bottom: none; }
.movers-row:hover { background: #fafafa; }
.movers-row:focus-visible { outline: 2px solid #000; outline-offset: -2px; }

.row-rank {
  font-size: 11px;
  font-weight: 700;
  color: #888;
  font-variant-numeric: tabular-nums;
}

.row-main {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.row-outcome {
  font-size: 13px;
  font-weight: 600;
  color: #000;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row-market {
  font-size: 10px;
  color: #777;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row-change {
  font-size: 13px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.row-change-up { color: var(--color-up); }
.row-change-down { color: var(--color-down); }

.col-empty {
  padding: 18px 14px;
  font-size: 12px;
  color: #aaa;
  text-align: center;
}

@media (max-width: 640px) {
  .movers-head {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }
  .movers-grid {
    grid-template-columns: 1fr;
  }
  .movers-col {
    border-right: none;
    border-bottom: 1px solid #e0e0e0;
  }
  .movers-col:last-child { border-bottom: none; }
}
</style>
