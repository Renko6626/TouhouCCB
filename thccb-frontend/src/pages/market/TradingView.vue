<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useMarketStore } from '@/stores/market'
import { useUserStore } from '@/stores/user'
import { useAuthStore } from '@/stores/auth'
import { 
  NButton, NCard, NGrid, NGridItem, NInputNumber, NSelect, 
  NStatistic, NTag, NSpace, NSpin, NAlert, NDivider,
  NForm, NFormItem, NRadioGroup, NRadio
} from 'naive-ui'
import type { SelectOption } from 'naive-ui'

const route = useRoute()
const marketStore = useMarketStore()
const userStore = useUserStore()
const authStore = useAuthStore()

// 状态
const loading = ref(false)
const tradeLoading = ref(false)
const marketId = computed(() => parseInt(route.params.id as string))

// 交易表单
const tradeType = ref<'buy' | 'sell'>('buy')
const selectedOutcomeId = ref<number | null>(null)
const shares = ref(1)
const quoteResult = ref<any>(null)

// 加载市场数据
const loadMarketData = async () => {
  if (!marketId.value) return
  
  loading.value = true
  try {
    await Promise.all([
      marketStore.fetchMarketDetail(marketId.value),
      marketStore.fetchMarketTrades(marketId.value)
    ])
    // 默认选择第一个选项
    if (marketStore.currentMarket?.outcomes?.length) {
      selectedOutcomeId.value = marketStore.currentMarket.outcomes[0].id
    }
  } finally {
    loading.value = false
  }
}

// 加载用户资产
const loadUserData = async () => {
  if (authStore.isAuthenticated) {
    await userStore.fetchSummary()
    await userStore.fetchHoldings()
  }
}

// 初始化加载
onMounted(() => {
  loadMarketData()
  loadUserData()
})

// 监听市场ID变化
watch(marketId, () => {
  loadMarketData()
})

// 获取选项选项
const outcomeOptions = computed<SelectOption[]>(() => {
  if (!marketStore.currentMarket?.outcomes) return []
  
  return marketStore.currentMarket.outcomes.map(outcome => ({
    label: `${outcome.label} (当前价格: ¥${outcome.current_price.toFixed(2)})`,
    value: outcome.id
  }))
})

// 获取当前选择的选项
const selectedOutcome = computed(() => {
  if (!selectedOutcomeId.value || !marketStore.currentMarket?.outcomes) return null
  return marketStore.currentMarket.outcomes.find(o => o.id === selectedOutcomeId.value)
})

// 获取用户在该选项的持仓
const userHolding = computed(() => {
  if (!selectedOutcomeId.value || !authStore.isAuthenticated) return null
  return userStore.getHoldingByOutcome(selectedOutcomeId.value)
})

// 计算最大可交易份额
const maxShares = computed(() => {
  if (tradeType.value === 'sell') {
    return userHolding.value?.amount || 0
  }
  
  // 买入时根据用户现金计算最大可买份额
  if (!selectedOutcome.value || !userStore.summary) return 100
  
  const cash = userStore.summary.cash
  const price = selectedOutcome.value.current_price
  return Math.floor(cash / price)
})

// 获取交易报价
const getQuote = async () => {
  if (!selectedOutcomeId.value || shares.value <= 0) {
    quoteResult.value = null
    return
  }

  try {
    const result = await marketStore.getQuote(
      selectedOutcomeId.value,
      shares.value,
      tradeType.value
    )
    if (result?.data) {
      // 计算衍生字段以保持兼容性
      quoteResult.value = {
        ...result.data,
        price_per_share: result.data.avg_price,  // 使用avg_price作为单价
        cost: result.data.net,                   // 使用net作为总成本
        new_cash: undefined                      // 需要前端计算
      }
    }
  } catch (error) {
    quoteResult.value = null
  }
}

// 计算交易后现金
const estimatedNewCash = computed(() => {
  if (!quoteResult.value || !userStore.summary) return 0
  const netCost = quoteResult.value.net || 0
  return tradeType.value === 'buy' 
    ? userStore.summary.cash - netCost
    : userStore.summary.cash + Math.abs(netCost)
})

// 监听交易参数变化
watch([tradeType, selectedOutcomeId, shares], () => {
  getQuote()
}, { immediate: true })

// 执行交易
const executeTrade = async () => {
  if (!selectedOutcomeId.value || shares.value <= 0) return
  
  tradeLoading.value = true
  try {
    if (tradeType.value === 'buy') {
      await marketStore.buyShares(selectedOutcomeId.value, shares.value)
    } else {
      await marketStore.sellShares(selectedOutcomeId.value, shares.value)
    }
    
    // 刷新数据
    await Promise.all([
      loadMarketData(),
      loadUserData()
    ])
    
    // 重置表单
    shares.value = 1
  } finally {
    tradeLoading.value = false
  }
}

// 快速交易份额选项
const quickShares = [1, 5, 10, 25, 50, 100]
</script>

<template>
  <div class="trading-view-page">
    <!-- 页面标题 -->
    <div class="mb-6">
      <h1 class="text-2xl font-bold text-gray-800 dark:text-gray-200 mb-2">
        交易视图 - {{ marketStore.currentMarket?.title || '加载中...' }}
      </h1>
      <p class="text-gray-600 dark:text-gray-400">
        买入或卖出市场选项份额
      </p>
    </div>

    <!-- 加载状态 -->
    <div v-if="loading" class="text-center py-12">
      <NSpin size="large" />
      <p class="mt-4 text-gray-600 dark:text-gray-400">加载市场数据中...</p>
    </div>

    <!-- 交易界面 -->
    <div v-else-if="marketStore.currentMarket" class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <!-- 左侧：市场信息 -->
      <div class="lg:col-span-2">
        <NCard title="市场信息" class="mb-6">
          <div class="space-y-4">
            <div>
              <h3 class="text-lg font-semibold mb-2">{{ marketStore.currentMarket.title }}</h3>
              <p class="text-gray-600 dark:text-gray-400">
                {{ marketStore.currentMarket.description }}
              </p>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div>
                <div class="text-sm text-gray-500">市场状态</div>
                <NTag :type="marketStore.currentMarket.status === 'trading' ? 'success' : 'warning'">
                  {{ marketStore.currentMarket.status === 'trading' ? '交易中' : '已暂停' }}
                </NTag>
              </div>
              <div>
                <div class="text-sm text-gray-500">流动性</div>
                <div class="text-lg font-semibold">
                  ¥{{ marketStore.currentMarket.liquidity_b.toLocaleString() }}
                </div>
              </div>
            </div>

            <!-- 选项列表 -->
            <div>
              <h4 class="font-semibold mb-3">市场选项</h4>
              <div class="space-y-3">
                <div 
                  v-for="outcome in marketStore.currentMarket.outcomes" 
                  :key="outcome.id"
                  class="p-3 border rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 cursor-pointer"
                  :class="{ 'border-primary-500 bg-primary-50 dark:bg-primary-900': selectedOutcomeId === outcome.id }"
                  @click="selectedOutcomeId = outcome.id"
                >
                  <div class="flex justify-between items-center">
                    <div>
                      <div class="font-medium">{{ outcome.label }}</div>
                      <div class="text-sm text-gray-500">
                        总份额: {{ outcome.total_shares.toLocaleString() }}
                      </div>
                    </div>
                    <div class="text-right">
                      <div class="text-lg font-bold text-primary-600 dark:text-primary-400">
                        ¥{{ outcome.current_price.toFixed(2) }}
                      </div>
                      <div class="text-sm text-gray-500">
                        赔付: ¥{{ outcome.payout?.toFixed(2) || '1.00' }}
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </NCard>

        <!-- 最近成交记录 -->
        <NCard title="最近成交" v-if="marketStore.marketTrades.length">
          <div class="space-y-2">
            <div 
              v-for="trade in marketStore.marketTrades.slice(0, 10)" 
              :key="trade.id"
              class="flex justify-between items-center p-2 border-b last:border-0"
            >
              <div>
                <span class="font-medium">
                  {{ marketStore.currentMarket?.outcomes?.find(o => o.id === trade.outcome_id)?.label || '未知选项' }}
                </span>
                <span class="text-sm text-gray-500 ml-2">{{ trade.side === 'buy' ? '买入' : '卖出' }}</span>
              </div>
              <div class="text-right">
                <div class="font-medium">{{ trade.shares }} 份额</div>
                <div class="text-sm text-gray-500">¥{{ trade.gross.toFixed(2) }}</div>
              </div>
            </div>
          </div>
        </NCard>
      </div>

      <!-- 右侧：交易面板 -->
      <div>
        <NCard title="交易面板">
          <NForm>
            <!-- 交易类型选择 -->
            <NFormItem label="交易类型">
              <NRadioGroup v-model:value="tradeType">
                <NSpace>
                  <NRadio value="buy">买入</NRadio>
                  <NRadio value="sell" :disabled="!userHolding">卖出</NRadio>
                </NSpace>
              </NRadioGroup>
            </NFormItem>

            <!-- 选项选择 -->
            <NFormItem label="选择选项">
              <NSelect
                v-model:value="selectedOutcomeId"
                :options="outcomeOptions"
                placeholder="请选择选项"
                :loading="loading"
              />
            </NFormItem>

            <!-- 份额输入 -->
            <NFormItem label="交易份额">
              <div class="space-y-3">
                <NInputNumber
                  v-model:value="shares"
                  :min="1"
                  :max="maxShares"
                  :step="1"
                  placeholder="输入交易份额"
                  class="w-full"
                />
                
                <!-- 快速选择 -->
                <div class="flex flex-wrap gap-2">
                  <NButton
                    v-for="amount in quickShares"
                    :key="amount"
                    size="small"
                    :type="shares === amount ? 'primary' : 'default'"
                    @click="shares = amount"
                    :disabled="amount > maxShares"
                  >
                    {{ amount }}
                  </NButton>
                  <NButton
                    size="small"
                    :type="shares === maxShares ? 'primary' : 'default'"
                    @click="shares = maxShares"
                  >
                    最大
                  </NButton>
                </div>
                
                <div class="text-sm text-gray-500">
                  最大可{{ tradeType === 'buy' ? '买入' : '卖出' }}: {{ maxShares }} 份额
                  <span v-if="userHolding && tradeType === 'sell'">
                    (当前持仓: {{ userHolding.amount }} 份额)
                  </span>
                </div>
              </div>
            </NFormItem>

            <NDivider />

            <!-- 交易报价 -->
            <div v-if="quoteResult" class="mb-4">
              <h4 class="font-semibold mb-2">交易预估</h4>
              <div class="space-y-2">
                <div class="flex justify-between">
                  <span>份额数量:</span>
                  <span class="font-medium">{{ shares }} 份额</span>
                </div>
                <div class="flex justify-between">
                  <span>单价:</span>
                  <span class="font-medium">¥{{ quoteResult.price_per_share?.toFixed(2) || '0.00' }}</span>
                </div>
                <div class="flex justify-between">
                  <span>总成本:</span>
                  <span class="font-medium text-primary-600 dark:text-primary-400">
                    ¥{{ quoteResult.cost?.toFixed(2) || '0.00' }}
                  </span>
                </div>
                <div class="flex justify-between">
                  <span>交易后现金:</span>
                  <span class="font-medium">¥{{ estimatedNewCash.toFixed(2) }}</span>
                </div>
              </div>
            </div>

            <!-- 用户资产信息 -->
            <div v-if="authStore.isAuthenticated && userStore.summary" class="mb-4">
              <h4 class="font-semibold mb-2">您的资产</h4>
              <div class="space-y-2">
                <div class="flex justify-between">
                  <span>现金:</span>
                  <span class="font-medium">¥{{ userStore.summary.cash.toFixed(2) }}</span>
                </div>
                <div class="flex justify-between">
                  <span>负债:</span>
                  <span class="font-medium">¥{{ userStore.summary.debt.toFixed(2) }}</span>
                </div>
                <div class="flex justify-between">
                  <span>持仓市值:</span>
                  <span class="font-medium">¥{{ userStore.summary.holdings_value.toFixed(2) }}</span>
                </div>
                <div class="flex justify-between border-t pt-2">
                  <span class="font-semibold">净值:</span>
                  <span class="font-bold text-green-600 dark:text-green-400">
                    ¥{{ userStore.summary.net_worth.toFixed(2) }}
                  </span>
                </div>
              </div>
            </div>

            <!-- 交易按钮 -->
            <div class="mt-6">
              <NButton
                type="primary"
                size="large"
                :loading="tradeLoading"
                :disabled="!selectedOutcomeId || shares <= 0 || shares > maxShares || !quoteResult"
                @click="executeTrade"
                class="w-full"
              >
                {{ tradeType === 'buy' ? '买入' : '卖出' }} {{ shares }} 份额
              </NButton>
              
              <div v-if="quoteResult" class="mt-2 text-center text-sm text-gray-500">
                预估成本: ¥{{ quoteResult.cost?.toFixed(2) || '0.00' }}
              </div>
            </div>
          </NForm>
        </NCard>

        <!-- 交易提示 -->
        <NAlert type="info" class="mt-4">
          <template #icon>
            <i class="i-mdi-information"></i>
          </template>
          <div class="text-sm">
            <div class="font-medium mb-1">交易提示:</div>
            <ul class="list-disc pl-4 space-y-1">
              <li>价格随市场供需动态变化</li>
              <li>买入份额后可在"我的资产"中查看</li>
              <li>市场结算后，获胜选项的份额将按赔付价格兑换</li>
            </ul>
          </div>
        </NAlert>
      </div>
    </div>

    <!-- 市场不存在 -->
    <div v-else class="text-center py-12">
      <n-empty description="市场不存在或已被删除">
        <template #extra>
          <NButton type="primary" @click="$router.push('/market/list')">
            返回市场列表
          </NButton>
        </template>
      </n-empty>
    </div>
  </div>
</template>

<style scoped>
.trading-view-page {
  max-width: 1400px;
  margin: 0 auto;
}
</style>