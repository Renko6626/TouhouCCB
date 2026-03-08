<script setup lang="ts">
import { ref, onMounted, computed, h } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { useMarketStore } from '@/stores/market'
import { 
  NButton, NCard, NGrid, NGridItem, NStatistic, NTag, 
  NSpace, NSpin, NDataTable, NEmpty, NAlert
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'

const router = useRouter()

const userStore = useUserStore()
const marketStore = useMarketStore()

// 状态
const loading = ref(false)

// 加载数据
const loadData = async () => {
  loading.value = true
  try {
    await Promise.all([
      userStore.fetchSummary(),
      userStore.fetchHoldings(),
      marketStore.fetchMarkets()
    ])
  } finally {
    loading.value = false
  }
}

// 初始化加载
onMounted(() => {
  loadData()
})

// 计算总持仓价值
const totalHoldingsValue = computed(() => {
  return userStore.totalHoldingsValue
})

// 持仓表格列定义
const holdingsColumns: DataTableColumns = [
  {
    title: '市场',
    key: 'market_title',
    width: 200
  },
  {
    title: '选项',
    key: 'outcome_label',
    width: 150
  },
  {
    title: '持仓份额',
    key: 'amount',
    width: 120,
    render: (row) => {
      return row.amount.toLocaleString()
    }
  },
  {
    title: '当前价格',
    key: 'current_price',
    width: 120,
    render: (row) => {
      const market = marketStore.markets.find(m => m.id === row.market_id)
      const outcome = market?.outcomes?.find(o => o.id === row.outcome_id)
      return outcome ? `¥${outcome.current_price.toFixed(2)}` : '-'
    }
  },
  {
    title: '持仓价值',
    key: 'value',
    width: 120,
    render: (row) => {
      const market = marketStore.markets.find(m => m.id === row.market_id)
      const outcome = market?.outcomes?.find(o => o.id === row.outcome_id)
      const value = outcome ? row.amount * outcome.current_price : 0
      return `¥${value.toFixed(2)}`
    }
  },
  {
    title: '市场状态',
    key: 'market_status',
    width: 100,
    render: (row) => {
      const market = marketStore.markets.find(m => m.id === row.market_id)
      if (!market) return '-'
      
      const type = market.status === 'trading' ? 'success' : 
                   market.status === 'halt' ? 'warning' : 'default'
      const text = market.status === 'trading' ? '交易中' : 
                   market.status === 'halt' ? '已暂停' : '已结算'
      
      return h(NTag, { type, size: 'small' }, { default: () => text })
    }
  },
  {
    title: '操作',
    key: 'actions',
    width: 150,
    render: (row) => {
      const market = marketStore.markets.find(m => m.id === row.market_id)
      const canTrade = market?.status === 'trading'
      
      return h(NSpace, {}, {
        default: () => [
          h(NButton, {
            size: 'small',
            type: 'primary',
            onClick: () => router.push(`/market/${row.market_id}`)
          }, { default: () => '查看市场' }),
          canTrade && h(NButton, {
            size: 'small',
            onClick: () => router.push(`/market/${row.market_id}/trade`)
          }, { default: () => '交易' })
        ]
      })
    }
  }
]

// 按市场分组持仓 - 转换为数组格式
const holdingsByMarketArray = computed(() => {
  const marketMap = userStore.holdingsByMarket
  return Array.from(marketMap.entries()).map(([marketId, holdings]) => {
    const market = marketStore.markets.find(m => m.id === marketId)
    return {
      market_id: marketId,
      market_title: market?.title || '未知市场',
      market_status: market?.status || 'unknown',
      holdings,
      total_value: holdings.reduce((sum, h) => {
        const outcome = market?.outcomes?.find(o => o.id === h.outcome_id)
        return sum + (outcome ? h.amount * outcome.current_price : 0)
      }, 0)
    }
  })
})
</script>

<template>
  <div class="portfolio-page">
    <!-- 页面标题 -->
    <div class="mb-6">
      <h1 class="text-2xl font-bold text-gray-800 dark:text-gray-200 mb-2">
        我的资产
      </h1>
      <p class="text-gray-600 dark:text-gray-400">
        查看您的资产组合和持仓情况
      </p>
    </div>

    <!-- 加载状态 -->
    <div v-if="loading && !userStore.summary" class="text-center py-12">
      <NSpin size="large" />
      <p class="mt-4 text-gray-600 dark:text-gray-400">加载资产数据中...</p>
    </div>

    <!-- 资产概览 -->
    <div v-else-if="userStore.summary" class="mb-6">
      <NGrid :cols="4" :x-gap="16">
        <NGridItem>
          <NCard>
            <NStatistic 
              label="现金余额" 
              :value="userStore.summary.cash" 
              :precision="2"
              prefix="¥"
            />
          </NCard>
        </NGridItem>
        <NGridItem>
          <NCard>
            <NStatistic 
              label="负债" 
              :value="userStore.summary.debt" 
              :precision="2"
              prefix="¥"
            />
          </NCard>
        </NGridItem>
        <NGridItem>
          <NCard>
            <NStatistic 
              label="持仓市值" 
              :value="totalHoldingsValue" 
              :precision="2"
              prefix="¥"
            />
          </NCard>
        </NGridItem>
        <NGridItem>
          <NCard>
            <NStatistic 
              label="净资产" 
              :value="userStore.summary.net_worth" 
              :precision="2"
              prefix="¥"
              class="text-green-600 dark:text-green-400"
            />
          </NCard>
        </NGridItem>
      </NGrid>

      <!-- 财富排名 -->
      <div class="mt-4">
        <NAlert type="info">
          <template #icon>
            <i class="i-mdi-trophy"></i>
          </template>
          <div class="flex items-center justify-between">
            <div>
              <div class="font-medium">财富排名: {{ userStore.summary.rank }}</div>
              <div class="text-sm text-gray-600 dark:text-gray-400">
                您的净资产在所有用户中排名 {{ userStore.summary.rank }}
              </div>
            </div>
            <NButton 
              type="primary" 
              size="small"
              @click="router.push('/market/leaderboard')"
            >
              查看排行榜
            </NButton>
          </div>
        </NAlert>
      </div>
    </div>

    <!-- 持仓详情 -->
    <div class="mt-8">
      <div class="flex justify-between items-center mb-4">
        <h2 class="text-xl font-bold text-gray-800 dark:text-gray-200">
          持仓明细
        </h2>
        <NButton 
          type="primary" 
          text
          @click="loadData"
          :loading="loading"
        >
          <template #icon>
            <i class="i-mdi-refresh"></i>
          </template>
          刷新
        </NButton>
      </div>

      <!-- 按市场分组显示 -->
      <div v-if="holdingsByMarketArray.length > 0" class="space-y-6">
        <NCard 
          v-for="marketHoldings in holdingsByMarketArray" 
          :key="marketHoldings.market_id"
          :title="marketHoldings.market_title"
        >
          <template #header-extra>
            <NTag :type="marketHoldings.market_status === 'trading' ? 'success' : 'warning'" size="small">
              {{ marketHoldings.market_status === 'trading' ? '交易中' : '已暂停' }}
            </NTag>
          </template>

          <NDataTable
            :columns="holdingsColumns"
            :data="marketHoldings.holdings"
            :bordered="false"
            size="small"
          />

          <template #footer>
            <div class="flex justify-between items-center text-sm text-gray-600 dark:text-gray-400">
              <div>该市场总持仓价值: ¥{{ marketHoldings.total_value.toFixed(2) }}</div>
              <NSpace>
                <NButton 
                  size="small"
                  @click="router.push(`/market/${marketHoldings.market_id}`)"
                >
                  查看市场详情
                </NButton>
                <NButton 
                  v-if="marketHoldings.market_status === 'trading'"
                  type="primary" 
                  size="small"
                  @click="router.push(`/market/${marketHoldings.market_id}/trade`)"
                >
                  交易该市场
                </NButton>
              </NSpace>
            </div>
          </template>
        </NCard>
      </div>

      <!-- 空状态 -->
      <div v-else class="text-center py-12">
        <NEmpty description="暂无持仓记录">
          <template #extra>
            <NButton type="primary" @click="router.push('/market/list')">
              去市场交易
            </NButton>
          </template>
        </NEmpty>
      </div>
    </div>

    <!-- 交易建议 -->
    <div v-if="userStore.holdings.length > 0" class="mt-8">
      <NAlert type="success">
        <template #icon>
          <i class="i-mdi-lightbulb-on"></i>
        </template>
        <div class="font-medium mb-2">交易建议</div>
        <ul class="list-disc pl-4 space-y-1 text-sm">
          <li>分散投资: 考虑在不同市场进行投资以降低风险</li>
          <li>定期检查: 关注市场动态和您的持仓价值变化</li>
          <li>止损策略: 设定合理的止损点，控制风险</li>
          <li>长期持有: 对于看好的市场，考虑长期持有以获得更高收益</li>
        </ul>
      </NAlert>
    </div>
  </div>
</template>

<style scoped>
.portfolio-page {
  max-width: 1200px;
  margin: 0 auto;
}
</style>