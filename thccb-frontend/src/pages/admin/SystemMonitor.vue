<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import { NButton, NDataTable, NTag, type DataTableColumns } from 'naive-ui'
import { useMarketStore } from '@/stores/market'
import { marketApi } from '@/api/market'
import type { LeaderboardItem, MarketTrade } from '@/types/api'

interface RecentTradeRow extends MarketTrade { market_id: number }

const marketStore = useMarketStore()
const loading = ref(false)
const recentTrades = ref<RecentTradeRow[]>([])
const healthMessage = ref('正常')
const lastUpdated = ref<string>('')

const summaryCards = computed(() => [
  { title: '活跃市场', value: marketStore.markets.length, sub: '来自 /api/v1/market/list' },
  { title: '排行榜样本用户', value: marketStore.leaderboard.length, sub: '来自 /api/v1/market/leaderboard' },
  { title: '最近成交(样本)', value: recentTrades.value.length, sub: `最多取前 10 个市场 · ${healthMessage.value}` },
])

const leaderboardColumns: DataTableColumns<LeaderboardItem> = [
  { title: '用户', key: 'username' },
  { title: '净值', key: 'net_worth', render: (row) => `¥${row.net_worth.toLocaleString()}` },
  { title: '称号', key: 'rank', render: (row) => h(NTag, { size: 'small', type: 'default' }, { default: () => row.rank }) },
]

const tradeColumns: DataTableColumns<RecentTradeRow> = [
  { title: '市场ID', key: 'market_id', width: 100, render: (row) => `#${row.market_id}` },
  { title: '选项ID', key: 'outcome_id', width: 100, render: (row) => `#${row.outcome_id}` },
  { title: '方向', key: 'side', width: 100, render: (row) => {
    const isBuy = row.side === 'buy'
    return h('span', {
      style: {
        display: 'inline-block', padding: '1px 8px', fontSize: '12px', fontWeight: '600',
        background: isBuy ? 'var(--color-up)' : 'var(--color-down)',
        color: '#fff', border: `1.5px solid ${isBuy ? 'var(--color-up)' : 'var(--color-down)'}`,
      }
    }, isBuy ? '买入' : '卖出')
  } },
  { title: '份额', key: 'shares', render: (row) => row.shares.toLocaleString() },
  { title: '价格', key: 'price', render: (row) => `¥${row.price.toFixed(4)}` },
  {
    title: '时间', key: 'timestamp', width: 180,
    render: (row) => new Date(row.timestamp).toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }),
  },
]

const loadRecentTrades = async () => {
  const sampleMarkets = marketStore.markets.slice(0, 10)
  const samples = await Promise.all(
    sampleMarkets.map(async (market) => {
      try {
        const rows = await marketApi.getMarketTrades(market.id, 10)
        return rows.map((item) => ({ ...item, market_id: market.id }))
      } catch { return [] as RecentTradeRow[] }
    }),
  )
  recentTrades.value = samples.flat()
    .sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime())
    .slice(0, 50)
}

const refreshStats = async () => {
  loading.value = true
  healthMessage.value = '更新中'
  try {
    await Promise.all([
      marketStore.fetchMarkets(),
      marketStore.fetchLeaderboard(20),
    ])
    await loadRecentTrades()
    healthMessage.value = '正常'
    lastUpdated.value = new Date().toISOString()
  } catch (error) {
    console.error('刷新系统统计失败:', error)
    healthMessage.value = `接口异常: ${error instanceof Error ? error.message : '未知错误'}`
  } finally {
    loading.value = false
  }
}

onMounted(() => refreshStats())
</script>

<template>
  <div class="monitor-page">
    <!-- 页头 -->
    <div class="page-bar">
      <div class="page-bar-left">
        <span class="page-bar-title">系统监控</span>
        <span class="page-bar-sub">基于现有接口汇总平台运行样本数据</span>
      </div>
      <NButton :loading="loading" @click="refreshStats">刷新数据</NButton>
    </div>

    <!-- 统计卡片 -->
    <div class="stats-grid">
      <div v-for="item in summaryCards" :key="item.title" class="stat-cell">
        <span class="stat-label">{{ item.title }}</span>
        <span class="stat-value">{{ item.value }}</span>
        <span class="stat-sub">{{ item.sub }}</span>
      </div>
    </div>
    <p class="last-updated">最后更新：{{ lastUpdated ? new Date(lastUpdated).toLocaleString('zh-CN') : '—' }}</p>

    <!-- 排行榜 -->
    <div class="content-panel">
      <div class="panel-heading">财富排行榜（Top 20）</div>
      <NDataTable :columns="leaderboardColumns" :data="marketStore.leaderboard" :loading="loading" :bordered="false" size="small" />
    </div>

    <!-- 最近成交 -->
    <div class="content-panel">
      <div class="panel-heading">最近成交（活跃市场样本）</div>
      <NDataTable :columns="tradeColumns" :data="recentTrades" :loading="loading" :bordered="false" size="small" />
    </div>
  </div>
</template>

<style scoped>
.monitor-page {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  border: 2px solid #000000;
  background: #ffffff;
}

.stat-cell {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  border-right: 1px solid #000000;
}

.stat-cell:last-child { border-right: none; }

.stat-label {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #888888;
}

.stat-value {
  font-size: 28px;
  font-weight: 900;
  color: #000000;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}

.stat-sub { font-size: 11px; color: #aaaaaa; }

.last-updated { font-size: 12px; color: #888888; margin-top: -8px; }

@media (max-width: 768px) {
  .stats-grid { grid-template-columns: repeat(1, 1fr); }
  .stat-cell { border-right: none; border-bottom: 1px solid #000000; }
  .stat-cell:last-child { border-bottom: none; }
}
</style>
