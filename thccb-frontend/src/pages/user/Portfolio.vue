<script setup lang="ts">
import { ref, onMounted, computed, h } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { useMarketStore } from '@/stores/market'
import {
  NButton, NCard, NTag,
  NSpace, NSpin, NDataTable, NEmpty, NAlert
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import type { Holding } from '@/types/api'
import MarketStatus from '@/components/market/MarketStatus.vue'

const router = useRouter()
const userStore = useUserStore()
const marketStore = useMarketStore()

const loading = ref(false)
const loadError = ref('')

const loadData = async () => {
  loading.value = true
  loadError.value = ''
  userStore.clearError()
  try {
    await Promise.all([
      userStore.fetchSummary(),
      userStore.fetchHoldings(),
      marketStore.fetchMarkets({ include_halt: true, include_settled: true }),
    ])
    if (userStore.error) {
      loadError.value = userStore.error
    }
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : '加载失败，请重试'
  } finally {
    loading.value = false
  }
}

onMounted(() => { loadData() })

const marketById = computed(() => {
  const map = new Map<number, any>()
  marketStore.markets.forEach(m => map.set(m.id, m))
  return map
})

// 浮盈亏颜色
const pnlColor = (val: number) => {
  if (val > 0) return 'var(--color-up)'
  if (val < 0) return 'var(--color-down)'
  return '#333'
}

const pnlSign = (val: number) => val > 0 ? '+' : ''

// 持仓表格列 — 直接用后端返回的 avg_price / current_price / unrealized_pnl
const holdingsColumns: DataTableColumns<Holding> = [
  { title: '选项', key: 'outcome_label', width: 140 },
  {
    title: '持仓',
    key: 'amount',
    width: 90,
    render: (row) => row.amount.toLocaleString(),
  },
  {
    title: '均价',
    key: 'avg_price',
    width: 100,
    render: (row) => `¥${row.avg_price.toFixed(4)}`,
  },
  {
    title: '现价',
    key: 'current_price',
    width: 100,
    render: (row) => `¥${row.current_price.toFixed(4)}`,
  },
  {
    title: '成本',
    key: 'cost_basis',
    width: 100,
    render: (row) => `¥${row.cost_basis.toFixed(2)}`,
  },
  {
    title: '市值',
    key: 'market_value',
    width: 100,
    render: (row) => `¥${row.market_value.toFixed(2)}`,
  },
  {
    title: '浮盈亏',
    key: 'unrealized_pnl',
    width: 110,
    render: (row) => {
      const val = row.unrealized_pnl
      return h('span', {
        style: { color: pnlColor(val), fontWeight: '700', fontVariantNumeric: 'tabular-nums' },
      }, `${pnlSign(val)}¥${val.toFixed(2)}`)
    },
  },
  {
    title: '操作',
    key: 'actions',
    width: 130,
    render: (row) => {
      const market = marketById.value.get(row.market_id)
      const canTrade = market?.status === 'trading'
      return h(NSpace, { size: 4 }, {
        default: () => [
          h(NButton, { size: 'small', onClick: () => router.push(`/market/${row.market_id}`) }, { default: () => '详情' }),
          canTrade && h(NButton, { size: 'small', type: 'primary', onClick: () => router.push(`/market/${row.market_id}/trade`) }, { default: () => '交易' }),
        ],
      })
    },
  },
]

// 按市场分组
const holdingsByMarketArray = computed(() => {
  const groupMap = userStore.holdingsByMarket
  return Array.from(groupMap.entries()).map(([mId, holdings]) => {
    const market = marketById.value.get(mId)
    const totalValue = holdings.reduce((sum, h) => sum + h.market_value, 0)
    const totalPnl = holdings.reduce((sum, h) => sum + h.unrealized_pnl, 0)
    return {
      market_id: mId,
      market_title: market?.title || holdings[0]?.market_title || '未知市场',
      market_status: market?.status || 'unknown',
      holdings,
      total_value: totalValue,
      total_pnl: totalPnl,
    }
  })
})
</script>

<template>
  <div class="portfolio-page">
    <!-- 加载状态 -->
    <div v-if="loading && !userStore.summary" class="loading-state">
      <NSpin size="large" />
      <p>加载资产数据中...</p>
    </div>

    <!-- 错误状态 -->
    <div v-else-if="loadError && !userStore.summary" class="py-8">
      <NAlert type="error" :title="loadError">
        <template #footer>
          <NButton size="small" @click="loadData">重新加载</NButton>
        </template>
      </NAlert>
    </div>

    <!-- 资产概览 -->
    <div v-else-if="userStore.summary">
      <div class="asset-grid">
        <div class="asset-card">
          <span class="asset-label">现金余额</span>
          <span class="asset-value">¥{{ userStore.summary.cash.toFixed(2) }}</span>
        </div>
        <div class="asset-card">
          <span class="asset-label">持仓成本</span>
          <span class="asset-value">¥{{ userStore.summary.total_cost_basis.toFixed(2) }}</span>
        </div>
        <div class="asset-card">
          <span class="asset-label">持仓市值</span>
          <span class="asset-value">¥{{ userStore.summary.holdings_value.toFixed(2) }}</span>
        </div>
        <div class="asset-card">
          <span class="asset-label">浮动盈亏</span>
          <span class="asset-value" :style="{ color: pnlColor(userStore.summary.unrealized_pnl) }">
            {{ pnlSign(userStore.summary.unrealized_pnl) }}¥{{ userStore.summary.unrealized_pnl.toFixed(2) }}
          </span>
        </div>
        <div class="asset-card asset-card-highlight asset-card-wide">
          <span class="asset-label">净资产</span>
          <span class="asset-value asset-value-net">¥{{ userStore.summary.net_worth.toFixed(2) }}</span>
        </div>
      </div>

      <!-- 称号 + 快捷操作 -->
      <div class="rank-bar">
        <div class="rank-info">
          <span class="rank-text">{{ userStore.summary.rank }}</span>
        </div>
        <div class="rank-actions">
          <NButton size="small" @click="router.push('/market/leaderboard')">排行榜</NButton>
          <NButton size="small" @click="router.push('/user/transactions')">交易记录</NButton>
        </div>
      </div>
    </div>

    <!-- 持仓详情 -->
    <div class="holdings-section">
      <div class="section-header">
        <h2 class="section-title">持仓明细</h2>
        <NButton :loading="loading" @click="loadData">刷新</NButton>
      </div>

      <div v-if="holdingsByMarketArray.length > 0" class="holdings-list">
        <NCard
          v-for="marketHoldings in holdingsByMarketArray"
          :key="marketHoldings.market_id"
          :title="marketHoldings.market_title"
        >
          <template #header-extra>
            <MarketStatus :status="marketHoldings.market_status" />
          </template>

          <NDataTable
            :columns="holdingsColumns"
            :data="marketHoldings.holdings"
            :bordered="false"
            :scroll-x="960"
            size="small"
          />

          <template #footer>
            <div class="card-footer-row">
              <div class="card-footer-stats">
                <span>市值：¥{{ marketHoldings.total_value.toFixed(2) }}</span>
                <span :style="{ color: pnlColor(marketHoldings.total_pnl), fontWeight: '700' }">
                  盈亏：{{ pnlSign(marketHoldings.total_pnl) }}¥{{ marketHoldings.total_pnl.toFixed(2) }}
                </span>
              </div>
              <NSpace size="small">
                <NButton size="small" @click="router.push(`/market/${marketHoldings.market_id}`)">市场详情</NButton>
                <NButton v-if="marketHoldings.market_status === 'trading'" type="primary" size="small" @click="router.push(`/market/${marketHoldings.market_id}/trade`)">去交易</NButton>
              </NSpace>
            </div>
          </template>
        </NCard>
      </div>

      <div v-else class="empty-state">
        <NEmpty description="暂无持仓记录">
          <template #extra>
            <NButton type="primary" @click="router.push('/market/list')">去市场交易</NButton>
          </template>
        </NEmpty>
      </div>
    </div>
  </div>
</template>

<style scoped>
.portfolio-page {
  max-width: 1200px;
  margin: 0 auto;
}

.loading-state {
  text-align: center;
  padding: 64px 0;
  color: #333;
  font-size: 13px;
}

/* 资产数字网格 — 5 格 */
.asset-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  border: 2px solid #000;
  background: #fff;
}

.asset-card {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  border-right: 1px solid #000;
  border-bottom: 1px solid #000;
}

.asset-card:nth-child(4) {
  border-right: none;
}

.asset-card-wide {
  grid-column: 1 / -1;
  border-right: none;
  border-bottom: none;
}

.asset-card-highlight {
  background: #000;
}

.asset-label {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #888;
}

.asset-card-highlight .asset-label {
  color: rgba(255, 255, 255, 0.55);
}

.asset-value {
  font-size: 22px;
  font-weight: 800;
  color: #000;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}

.asset-value-net {
  color: #fff;
}

/* 称号栏 */
.rank-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border: 2px solid #000;
  border-top: none;
  background: #f5f5f5;
}

.rank-info {
  display: flex;
  align-items: center;
  font-size: 14px;
  font-weight: 600;
  color: #000;
}

.rank-text {
  font-weight: 700;
}

.rank-actions {
  display: flex;
  gap: 8px;
}

/* 持仓区 */
.holdings-section {
  margin-top: 32px;
}

.holdings-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 2px solid #000;
  padding-bottom: 10px;
  margin-bottom: 16px;
}

.section-title {
  font-size: 15px;
  font-weight: 700;
  color: #000;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.card-footer-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
}

.card-footer-stats {
  display: flex;
  gap: 20px;
  font-variant-numeric: tabular-nums;
}

.empty-state {
  text-align: center;
  padding: 48px 0;
}

@media (max-width: 768px) {
  .asset-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  .asset-card:nth-child(2) {
    border-right: none;
  }
  .asset-card-wide {
    grid-column: 1 / -1;
  }
}
</style>
