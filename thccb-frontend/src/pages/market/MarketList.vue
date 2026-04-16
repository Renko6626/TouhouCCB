<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useMarketStore } from '@/stores/market'
import { NInput, NSelect, NPagination, NSpin, NEmpty, NButton } from 'naive-ui'
import type { SelectOption } from 'naive-ui'
import MarketCard from '@/components/market/MarketCard.vue'

const router = useRouter()
const marketStore = useMarketStore()

const loading = ref(false)
const searchQuery = ref('')
const statusFilter = ref<string>('trading')
const sortBy = ref<string>('default')
const currentPage = ref(1)
const pageSize = 12

const statusOptions: SelectOption[] = [
  { label: '交易中', value: 'trading' },
  { label: '已暂停', value: 'halt' },
  { label: '已结算', value: 'settled' },
  { label: '全部状态', value: 'all' },
]

const sortOptions: SelectOption[] = [
  { label: '默认排序', value: 'default' },
  { label: '流动性 高→低', value: 'liquidity_desc' },
  { label: '流动性 低→高', value: 'liquidity_asc' },
  { label: '选项数 多→少', value: 'outcomes_desc' },
]

// 根据筛选条件构建后端查询参数
const buildParams = () => {
  const params: Record<string, any> = {}
  if (searchQuery.value.trim()) {
    params.keyword = searchQuery.value.trim()
  }
  // 后端默认只返回 trading，需要额外标志才返回 halt/settled
  if (statusFilter.value === 'all') {
    params.include_halt = true
    params.include_settled = true
  } else if (statusFilter.value === 'halt') {
    params.include_halt = true
  } else if (statusFilter.value === 'settled') {
    params.include_settled = true
  }
  // trading 是默认行为，不需要额外参数
  return params
}

const loadMarkets = async () => {
  loading.value = true
  try {
    await marketStore.fetchMarkets(buildParams())
  } finally {
    loading.value = false
  }
}

onMounted(() => { loadMarkets() })

// 筛选条件变化时重新从后端拉取
watch([searchQuery, statusFilter], () => {
  currentPage.value = 1
  loadMarkets()
})

// 对于只筛选单个状态的情况，后端可能返回多个状态，前端再做一次精确过滤
const filteredMarkets = computed(() => {
  let markets = [...marketStore.markets]

  // 如果选了具体状态，确保只显示该状态（后端 include_halt 会同时返回 trading+halt）
  if (statusFilter.value !== 'all') {
    markets = markets.filter(m => m.status === statusFilter.value)
  }

  // 排序（纯前端）
  if (sortBy.value === 'liquidity_desc') {
    markets.sort((a, b) => (b.liquidity_b ?? 0) - (a.liquidity_b ?? 0))
  } else if (sortBy.value === 'liquidity_asc') {
    markets.sort((a, b) => (a.liquidity_b ?? 0) - (b.liquidity_b ?? 0))
  } else if (sortBy.value === 'outcomes_desc') {
    markets.sort((a, b) => (b.outcomes?.length ?? 0) - (a.outcomes?.length ?? 0))
  }
  return markets
})

const paginatedMarkets = computed(() => {
  const start = (currentPage.value - 1) * pageSize
  return filteredMarkets.value.slice(start, start + pageSize)
})

const totalPages = computed(() => Math.ceil(filteredMarkets.value.length / pageSize))

watch(sortBy, () => { currentPage.value = 1 })

const handleView = (id: number) => router.push(`/market/${id}/trade`)
const handleTrade = (id: number) => router.push(`/market/${id}/trade`)
</script>

<template>
  <div class="market-list-page">

    <!-- 工具栏 -->
    <div class="filter-bar">
      <div class="search-input">
        <NInput
          v-model:value="searchQuery"
          placeholder="搜索市场名称..."
          clearable
          style="width: 100%"
        >
          <template #prefix>
            <span style="font-weight:700; color:#000;">Q</span>
          </template>
        </NInput>
      </div>
      <div class="filter-right">
        <NSelect
          v-model:value="sortBy"
          :options="sortOptions"
          style="width: 160px"
        />
        <NSelect
          v-model:value="statusFilter"
          :options="statusOptions"
          style="width: 140px"
        />
        <NButton :loading="loading" @click="loadMarkets">
          刷新
        </NButton>
      </div>
    </div>

    <!-- 结果统计 -->
    <div class="result-count">
      共 <strong>{{ filteredMarkets.length }}</strong> 个市场
      <span v-if="searchQuery" class="filter-tag">关键词: "{{ searchQuery }}"</span>
      <span v-if="statusFilter !== 'trading'" class="filter-tag">
        {{ statusOptions.find(o => o.value === statusFilter)?.label }}
      </span>
    </div>

    <!-- 加载中 -->
    <div v-if="loading && !marketStore.markets.length" class="loading-state">
      <NSpin size="large" />
      <p>加载市场中...</p>
    </div>

    <!-- 市场列表 -->
    <div v-else>
      <div v-if="paginatedMarkets.length" class="market-grid">
        <MarketCard
          v-for="market in paginatedMarkets"
          :key="market.id"
          :market="market"
          @view="handleView"
          @trade="handleTrade"
        />
      </div>

      <div v-else class="empty-state">
        <NEmpty description="没有找到匹配的市场">
          <template #extra>
            <NButton @click="searchQuery = ''; statusFilter = 'trading'; loadMarkets()">清除筛选</NButton>
          </template>
        </NEmpty>
      </div>

      <div v-if="filteredMarkets.length > pageSize" class="pagination-bar">
        <NPagination
          v-model:page="currentPage"
          :page-count="totalPages"
          :page-size="pageSize"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.market-list-page {
  max-width: 1200px;
  margin: 0 auto;
}

.search-input { flex: 1; min-width: 0; }

.filter-right {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-shrink: 0;
}

.filter-tag {
  display: inline-block;
  margin-left: 8px;
  padding: 1px 6px;
  border: 1px solid #000000;
  font-size: 11px;
  background: #f5f5f5;
}

.loading-state {
  text-align: center;
  padding: 64px 0;
  color: #666666;
  font-size: 13px;
}

.market-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
  margin-bottom: 24px;
}

.pagination-bar {
  display: flex;
  justify-content: center;
  padding: 16px 0;
  border-top: 2px solid #000000;
  margin-top: 8px;
}

@media (max-width: 768px) {
  .market-grid { grid-template-columns: 1fr; }
  .filter-right { width: 100%; justify-content: flex-end; }
}

@media (min-width: 1200px) {
  .market-grid { grid-template-columns: repeat(3, 1fr); }
}
</style>
