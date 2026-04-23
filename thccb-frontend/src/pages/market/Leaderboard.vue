<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NButton, NDataTable, NInputNumber, NSpin, NTag, NEmpty, type DataTableColumns } from 'naive-ui'
import type { LeaderboardItem } from '@/types/api'
import { useMarketStore } from '@/stores/market'

const marketStore = useMarketStore()
const limit = ref(20)
const loading = ref(false)
const leaderboardRows = computed(() => marketStore.leaderboard)

// 前 3 名徽章样式（工业风黑白递进；不用金银铜彩色，守 docs/style.md）
const rankBadgeStyle = (rank: number): Record<string, string> => {
  const base = {
    display: 'inline-block',
    minWidth: '34px',
    padding: '1px 8px',
    fontSize: '12px',
    fontWeight: '800',
    letterSpacing: '0.04em',
    textAlign: 'center',
    border: '1.5px solid #000',
    fontVariantNumeric: 'tabular-nums',
  }
  if (rank === 1) return { ...base, background: '#000', color: '#fff' }
  if (rank === 2) return { ...base, background: '#444', color: '#fff' }
  if (rank === 3) return { ...base, background: '#888', color: '#fff' }
  return { ...base, background: '#fff', color: '#000', fontWeight: '600' }
}

const columns: DataTableColumns<LeaderboardItem> = [
  {
    title: '排名', key: 'rankIndex', width: 90,
    render: (_row, index) =>
      h('span', { style: rankBadgeStyle(index + 1) }, `#${index + 1}`),
  },
  { title: '用户', key: 'username' },
  { title: '净值', key: 'net_worth', render: (row) => `¥${row.net_worth.toLocaleString()}` },
  { title: '称号', key: 'rank', render: (row) => h(NTag, { type: 'default', size: 'small' }, { default: () => row.rank }) },
]

const loadLeaderboard = async () => {
  loading.value = true
  try {
    await marketStore.fetchLeaderboard(limit.value)
  } finally {
    loading.value = false
  }
}

// limit 变化 300ms 防抖自动刷新（避免 NInputNumber 按 step 连打时每步一发）
let limitDebounceTimer: ReturnType<typeof setTimeout> | null = null
watch(limit, () => {
  if (limitDebounceTimer) clearTimeout(limitDebounceTimer)
  limitDebounceTimer = setTimeout(loadLeaderboard, 300)
})

onBeforeUnmount(() => {
  if (limitDebounceTimer) clearTimeout(limitDebounceTimer)
})

onMounted(() => loadLeaderboard())
</script>

<template>
  <div class="leaderboard-page">
    <div class="page-bar">
      <div class="page-bar-left">
        <span class="page-bar-title">财富排行榜</span>
        <span class="page-bar-sub">平台净值排名</span>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <NInputNumber v-model:value="limit" :min="5" :max="100" :step="5" size="small" style="width:100px" />
        <NButton :loading="loading" @click="loadLeaderboard">刷新</NButton>
      </div>
    </div>

    <div class="content-panel">
      <div v-if="loading && !leaderboardRows.length" class="lb-loading">
        <NSpin size="large" />
        <p>加载排行榜中...</p>
      </div>
      <div v-else-if="!leaderboardRows.length" class="empty-state">
        <NEmpty description="暂无排行榜数据" />
      </div>
      <NDataTable v-else :columns="columns" :data="leaderboardRows" :loading="loading" :bordered="false" size="small" />
    </div>
  </div>
</template>

<style scoped>
.leaderboard-page {
  max-width: 1000px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 0;
}

.lb-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 48px 0;
  font-size: 13px;
  color: #888888;
}
</style>
