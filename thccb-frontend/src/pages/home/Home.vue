<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useMarketStore } from '@/stores/market'
import { useAuthStore } from '@/stores/auth'
import { NButton, NCard, NGrid, NGridItem, NStatistic, NSpace } from 'naive-ui'

const marketStore = useMarketStore()
const authStore = useAuthStore()

// 加载市场数据
onMounted(async () => {
  if (!marketStore.markets.length) {
    await marketStore.fetchMarkets()
  }
})

// 获取活跃市场
const activeMarkets = ref([])
const loading = ref(false)

// 获取市场数据
const loadMarkets = async () => {
  loading.value = true
  try {
    await marketStore.fetchMarkets()
    activeMarkets.value = marketStore.activeMarkets.slice(0, 4) // 只显示前4个
  } finally {
    loading.value = false
  }
}

// 初始化加载
loadMarkets()
</script>

<template>
  <div class="home-page">
    <!-- 英雄区域 -->
    <div class="hero-section mb-8">
      <div class="text-center py-12">
        <h1 class="text-4xl font-bold text-gray-800 dark:text-gray-200 mb-4">
          东方炒炒币预测市场
        </h1>
        <p class="text-xl text-gray-600 dark:text-gray-400 mb-8 max-w-2xl mx-auto">
          一个基于东方Project的预测市场交易平台，交易您对未来的预测，赚取收益
        </p>
        <div class="flex justify-center space-x-4">
          <NButton 
            v-if="!authStore.isAuthenticated" 
            type="primary" 
            size="large"
            @click="$router.push('/auth/login')"
          >
            立即登录
          </NButton>
          <NButton 
            v-if="!authStore.isAuthenticated" 
            type="default" 
            size="large"
            @click="$router.push('/auth/register')"
          >
            注册账号
          </NButton>
          <NButton 
            v-if="authStore.isAuthenticated" 
            type="primary" 
            size="large"
            @click="$router.push('/market/list')"
          >
            开始交易
          </NButton>
        </div>
      </div>
    </div>

    <!-- 统计信息 -->
    <div v-if="authStore.isAuthenticated" class="stats-section mb-8">
      <NGrid :cols="4" :x-gap="16">
        <NGridItem>
          <NCard>
            <NStatistic label="活跃市场" :value="marketStore.activeMarkets.length" />
          </NCard>
        </NGridItem>
        <NGridItem>
          <NCard>
            <NStatistic label="总市场数" :value="marketStore.markets.length" />
          </NCard>
        </NGridItem>
        <NGridItem>
          <NCard>
            <NStatistic label="交易中" :value="marketStore.activeMarkets.filter(m => m.status === 'trading').length" />
          </NCard>
        </NGridItem>
        <NGridItem>
          <NCard>
            <NStatistic label="已结算" :value="marketStore.markets.filter(m => m.status === 'settled').length" />
          </NCard>
        </NGridItem>
      </NGrid>
    </div>

    <!-- 热门市场 -->
    <div class="markets-section">
      <div class="flex justify-between items-center mb-6">
        <h2 class="text-2xl font-bold text-gray-800 dark:text-gray-200">
          热门市场
        </h2>
        <NButton 
          type="primary" 
          text
          @click="$router.push('/market/list')"
        >
          查看全部
        </NButton>
      </div>

      <div v-if="loading" class="text-center py-8">
        <n-spin size="large" />
      </div>

      <NGrid v-else :cols="2" :x-gap="16" :y-gap="16">
        <NGridItem v-for="market in activeMarkets" :key="market.id">
          <NCard :title="market.title" hoverable>
            <template #header-extra>
              <n-tag :type="market.status === 'trading' ? 'success' : 'warning'">
                {{ market.status === 'trading' ? '交易中' : '已暂停' }}
              </n-tag>
            </template>
            
            <p class="text-gray-600 dark:text-gray-400 mb-4 line-clamp-2">
              {{ market.description }}
            </p>
            
            <div class="flex justify-between items-center">
              <div class="text-sm text-gray-500">
                流动性: ¥{{ market.liquidity_b.toLocaleString() }}
              </div>
              <NButton 
                type="primary" 
                size="small"
                @click="$router.push(`/market/${market.id}`)"
              >
                查看详情
              </NButton>
            </div>
          </NCard>
        </NGridItem>
      </NGrid>

      <div v-if="!loading && activeMarkets.length === 0" class="text-center py-8">
        <n-empty description="暂无活跃市场">
          <template #extra>
            <NButton 
              v-if="authStore.isAdmin" 
              type="primary"
              @click="$router.push('/admin/market-manage')"
            >
              创建市场
            </NButton>
          </template>
        </n-empty>
      </div>
    </div>

    <!-- 功能说明 -->
    <div class="features-section mt-12">
      <h2 class="text-2xl font-bold text-gray-800 dark:text-gray-200 mb-6 text-center">
        平台特色
      </h2>
      
      <NGrid :cols="3" :x-gap="16">
        <NGridItem>
          <NCard>
            <div class="text-center">
              <div class="w-12 h-12 bg-primary-100 dark:bg-primary-900 rounded-full flex items-center justify-center mx-auto mb-4">
                <i class="i-mdi-chart-line text-primary-600 dark:text-primary-300 text-2xl"></i>
              </div>
              <h3 class="text-lg font-semibold mb-2">预测交易</h3>
              <p class="text-gray-600 dark:text-gray-400">
                交易您对未来事件的预测，通过市场机制发现真实概率
              </p>
            </div>
          </NCard>
        </NGridItem>
        
        <NGridItem>
          <NCard>
            <div class="text-center">
              <div class="w-12 h-12 bg-green-100 dark:bg-green-900 rounded-full flex items-center justify-center mx-auto mb-4">
                <i class="i-mdi-cash-multiple text-green-600 dark:text-green-300 text-2xl"></i>
              </div>
              <h3 class="text-lg font-semibold mb-2">实时交易</h3>
              <p class="text-gray-600 dark:text-gray-400">
                实时买卖份额，价格随市场供需动态变化
              </p>
            </div>
          </NCard>
        </NGridItem>
        
        <NGridItem>
          <NCard>
            <div class="text-center">
              <div class="w-12 h-12 bg-purple-100 dark:bg-purple-900 rounded-full flex items-center justify-center mx-auto mb-4">
                <i class="i-mdi-shield-account text-purple-600 dark:text-purple-300 text-2xl"></i>
              </div>
              <h3 class="text-lg font-semibold mb-2">安全可靠</h3>
              <p class="text-gray-600 dark:text-gray-400">
                基于LMSR算法，确保市场流动性和公平性
              </p>
            </div>
          </NCard>
        </NGridItem>
      </NGrid>
    </div>
  </div>
</template>

<style scoped>
.home-page {
  max-width: 1200px;
  margin: 0 auto;
}

.hero-section {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  border-radius: 12px;
  color: white;
}

.hero-section h1,
.hero-section p {
  color: white;
}

.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>