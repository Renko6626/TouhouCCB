<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NDataTable, NInputNumber, NSpin, NTag, NEmpty, type DataTableColumns } from 'naive-ui'
import type { LeaderboardItem, LeaderboardMode } from '@/types/api'
import { useMarketStore } from '@/stores/market'

const marketStore = useMarketStore()
const limit = ref(20)
const mode = ref<LeaderboardMode>('net_worth')
const loading = ref(false)
const loadError = ref('')
const leaderboardRows = computed(() => marketStore.leaderboard)

const modeOptions: { value: LeaderboardMode; label: string; sub: string }[] = [
  { value: 'net_worth', label: '净值榜', sub: '按 现金 - 债务' },
  { value: 'spending', label: '消费榜', sub: '按 兑换消费 - 债务' },
]

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

const numStyle = { fontVariantNumeric: 'tabular-nums' as const }

// 列定义随 mode 动态切换：net_worth 模式只显示总分；spending 模式额外展示消费/债务分量
const columns = computed<DataTableColumns<LeaderboardItem>>(() => {
  const base: DataTableColumns<LeaderboardItem> = [
    {
      title: '排名', key: 'rankIndex', width: 90,
      render: (_row, index) =>
        h('span', { style: rankBadgeStyle(index + 1) }, `#${index + 1}`),
    },
    { title: '用户', key: 'username' },
  ]
  if (mode.value === 'spending') {
    base.push(
      {
        title: '消费总额', key: 'spent_total',
        render: (row) => h('span', { style: numStyle },
          row.spent_total != null ? `金 ${Number(row.spent_total).toLocaleString()}` : '—'),
      },
      {
        title: '当前债务', key: 'debt',
        render: (row) => h('span', { style: numStyle },
          row.debt != null ? `金 ${Number(row.debt).toLocaleString()}` : '—'),
      },
      {
        title: '净消费', key: 'net_worth',
        render: (row) => h('span',
          { style: { ...numStyle, fontWeight: '700' } },
          `金 ${row.net_worth.toLocaleString()}`),
      },
    )
  } else {
    base.push({
      title: '净值', key: 'net_worth',
      render: (row) => h('span', { style: numStyle }, `金 ${row.net_worth.toLocaleString()}`),
    })
  }
  base.push({
    title: '称号', key: 'rank',
    render: (row) => h(NTag, { type: 'default', size: 'small' }, { default: () => row.rank }),
  })
  return base
})

const loadLeaderboard = async () => {
  loading.value = true
  loadError.value = ''
  try {
    await marketStore.fetchLeaderboard(limit.value, mode.value)
    if (marketStore.error) {
      loadError.value = marketStore.error
    }
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : '加载排行榜失败'
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

// mode 切换立即重拉
watch(mode, () => loadLeaderboard())

onBeforeUnmount(() => {
  if (limitDebounceTimer) clearTimeout(limitDebounceTimer)
})

onMounted(() => loadLeaderboard())
</script>

<template>
  <div class="leaderboard-page">
    <div class="page-bar">
      <div class="page-bar-left">
        <span class="page-bar-title">排行榜</span>
        <span class="page-bar-sub">
          {{ modeOptions.find(o => o.value === mode)?.sub }}
        </span>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <NInputNumber v-model:value="limit" :min="5" :max="100" :step="5" size="small" style="width:100px" />
        <NButton :loading="loading" @click="loadLeaderboard">刷新</NButton>
      </div>
    </div>

    <!-- mode 切换 tab -->
    <div class="mode-tabs">
      <button
        v-for="opt in modeOptions"
        :key="opt.value"
        type="button"
        class="mode-tab"
        :class="{ 'mode-tab--active': mode === opt.value }"
        @click="mode = opt.value"
      >
        {{ opt.label }}
      </button>
    </div>

    <div class="content-panel">
      <div v-if="loading && !leaderboardRows.length" class="lb-loading">
        <NSpin size="large" />
        <p>正在加载…</p>
      </div>
      <div v-else-if="loadError && !leaderboardRows.length" class="lb-error">
        <NAlert type="error" :title="loadError">
          <div style="margin-top:8px">
            <NButton size="small" @click="loadLeaderboard">重新加载</NButton>
          </div>
        </NAlert>
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

.mode-tabs {
  display: flex;
  gap: 0;
  margin-bottom: 12px;
  border-bottom: 2px solid #000;
}

.mode-tab {
  appearance: none;
  background: transparent;
  border: 2px solid #000;
  border-bottom: none;
  padding: 8px 18px;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: #000;
  cursor: pointer;
  margin-right: -2px;
  margin-bottom: -2px;
  transition: background 0.1s;
}

.mode-tab:hover {
  background: #f0f0f0;
}

.mode-tab--active {
  background: #000;
  color: #fff;
}
</style>
