<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useMarketStore } from '@/stores/market'
import { NButton, NCard, NGrid, NGridItem, NInput, NSelect, NPagination, NTag, NSpace, NSpin, NEmpty } from 'naive-ui'
import type { SelectOption } from 'naive-ui'

const marketStore = useMarketStore()

// 状态
const loading = ref(false)
const searchQuery = ref('')
const statusFilter = ref<string>('all')
const currentPage = ref(1)
const pageSize = 12

// 状态筛选选项
const statusOptions: SelectOption[] = [
  { label: '全部', value: 'all' },
  { label: '交易中', value: 'trading' },
  { label: '已暂停', value: 'halt' },
  { label: '已结算', value: 'settled' }
]

// 加载市场数据
const loadMarkets = async () => {
  loading.value = true
  try {
    await marketStore.fetchMarkets()
  } finally {
    loading.value = false
  }
}

// 初始化加载
onMounted(() => {
  loadMarkets()
})

// 过滤后的市场列表
const filteredMarkets = computed(() => {
  let markets = marketStore.markets

  // 搜索过滤
  if (searchQuery.value) {
    const query = searchQuery.value.toLowerCase()
    markets = markets.filter(market => 
      market.title.toLowerCase().includes(query) || 
      market.description?.toLowerCase().includes(query)
    )
  }

  // 状态过滤
  if (statusFilter.value && statusFilter.value !== 'all') {
    markets = markets.filter(market => market.status === statusFilter.value)
  }

  return markets
})

// 分页后的市场列表
const paginatedMarkets = computed(() => {
  const start = (currentPage.value - 1) * pageSize
  const end = start + pageSize
  return filteredMarkets.value.slice(start, end)
})

// 总页数
const totalPages = computed(() => {
  return Math.ceil(filteredMarkets.value.length / pageSize)
})

// 获取状态标签类型
const getStatusType = (status: string) => {
  switch (status) {
    case 'trading': return 'success'
    case 'halt': return 'warning'
    case 'settled': return 'default'
    default: return 'default'
  }
}

// 获取状态文本
const getStatusText = (status: string) => {
  switch (status) {
    case 'trading': return '交易中'
    case 'halt': return '已暂停'
    case 'settled': return '已结算'
    default: return status
  }
}
</script>

<template>
  <div class="market-list-page">
    <!-- 页面标题和操作 -->
    <div class="mb-6">
      <h1 class="text-2xl font-bold text-gray-800 dark:text-gray-200 mb-2">
        市场列表
      </h1>
      <p class="text-gray-600 dark:text-gray-400">
        浏览所有预测市场，选择您感兴趣的市场进行交易
      </p>
    </div>

    <!-- 筛选工具栏 -->
    <div class="bg-white dark:bg-gray-800 rounded-lg shadow-sm p-4 mb-6">
      <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
        <div class="flex-1">
          <NInput
            v-model:value="searchQuery"
            placeholder="搜索市场名称或描述..."
            clearable
          >
            <template #prefix>
              <i class="i-mdi-magnify"></i>
            </template>
          </NInput>
        </div>
        
        <div class="flex items-center space-x-4">
          <div class="w-48">
            <NSelect
              v-model:value="statusFilter"
              :options="statusOptions"
              placeholder="筛选状态"
              clearable
            />
          </div>
          
          <NButton 
            type="primary" 
            @click="loadMarkets"
            :loading="loading"
          >
            <template #icon>
              <i class="i-mdi-refresh"></i>
            </template>
            刷新
          </NButton>
        </div>
      </div>
    </div>

    <!-- 加载状态 -->
    <div v-if="loading && !marketStore.markets.length" class="text-center py-12">
      <NSpin size="large" />
      <p class="mt-4 text-gray-600 dark:text-gray-400">加载市场中...</p>
    </div>

    <!-- 市场网格 -->
    <div v-else>
      <!-- 市场统计 -->
      <div class="mb-4 text-sm text-gray-600 dark:text-gray-400">
        共 {{ filteredMarkets.length }} 个市场
        <span v-if="searchQuery">（搜索: "{{ searchQuery }}"）</span>
        <span v-if="statusFilter">（状态: {{ getStatusText(statusFilter) }}）</span>
      </div>

      <!-- 市场卡片 -->
      <NGrid :cols="1" :x-gap="16" :y-gap="16" class="mb-6">
        <NGridItem v-for="market in paginatedMarkets" :key="market.id">
          <NCard :title="market.title" hoverable>
            <template #header-extra>
              <NTag :type="getStatusType(market.status)" size="small">
                {{ getStatusText(market.status) }}
              </NTag>
            </template>

            <div class="mb-4">
              <p class="text-gray-600 dark:text-gray-400 line-clamp-2">
                {{ market.description || '暂无描述' }}
              </p>
            </div>

            <div class="grid grid-cols-2 gap-4 mb-4">
              <div>
                <div class="text-sm text-gray-500">流动性</div>
                <div class="text-lg font-semibold">
                  ¥{{ market.liquidity_b.toLocaleString() }}
                </div>
              </div>
              <div>
                <div class="text-sm text-gray-500">选项数量</div>
                <div class="text-lg font-semibold">
                  {{ market.outcomes?.length || 0 }}
                </div>
              </div>
            </div>

            <div class="flex justify-between items-center">
              <div class="text-sm text-gray-500">
                选项数量: {{ market.outcomes?.length || 0 }}
              </div>
              <NSpace>
                <NButton 
                  type="primary" 
                  size="small"
                  @click="$router.push(`/market/${market.id}`)"
                >
                  查看详情
                </NButton>
                <NButton 
                  v-if="market.status === 'trading'"
                  type="default" 
                  size="small"
                  @click="$router.push(`/market/${market.id}/trade`)"
                >
                  开始交易
                </NButton>
              </NSpace>
            </div>
          </NCard>
        </NGridItem>
      </NGrid>

      <!-- 空状态 -->
      <div v-if="filteredMarkets.length === 0" class="text-center py-12">
        <NEmpty description="没有找到匹配的市场">
          <template #extra>
            <NButton type="primary" @click="searchQuery = ''; statusFilter = 'all'">
              清除筛选
            </NButton>
          </template>
        </NEmpty>
      </div>

      <!-- 分页 -->
      <div v-if="filteredMarkets.length > pageSize" class="flex justify-center">
        <NPagination
          v-model:page="currentPage"
          :page-count="totalPages"
          :page-size="pageSize"
          show-size-picker
          :page-sizes="[12, 24, 36, 48]"
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

.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>