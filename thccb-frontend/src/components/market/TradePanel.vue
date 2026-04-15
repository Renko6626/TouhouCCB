<script setup lang="ts">
import { ref, computed } from 'vue'
import { 
  NButton, NCard, NForm, NFormItem, NInputNumber, NSelect,
  NRadioGroup, NRadio, NSpace, NAlert
} from 'naive-ui'
import type { SelectOption } from 'naive-ui'
import { useUserStore } from '@/stores/user'
import { useAuthStore } from '@/stores/auth'
import { useMarketStore } from '@/stores/market'
import type { MarketDetail, OutcomeQuote, QuoteResponse, Holding } from '@/types/api'

const props = defineProps<{
  market: MarketDetail | null
  selectedOutcomeId: number | null
  tradeType: 'buy' | 'sell'
  shares: number
  maxShares: number
  quoteResult: QuoteResponse | null
  estimatedNewCash: number
  userHolding: Holding | null
  quoteExceedsCash: boolean
}>()

const emit = defineEmits<{
  'update:selectedOutcomeId': [value: number | null]
  'update:tradeType': [value: 'buy' | 'sell']
  'update:shares': [value: number]
  'executeTrade': []
}>()

const userStore = useUserStore()
const authStore = useAuthStore()
const marketStore = useMarketStore()

// 状态
const loading = ref(false)

// 计算属性
const outcomeOptions = computed<SelectOption[]>(() => {
  if (!props.market?.outcomes) return []
  
  return props.market.outcomes.map((outcome: OutcomeQuote) => ({
    label: `${outcome.label} (当前价格: ¥${outcome.current_price.toFixed(2)})`,
    value: outcome.id
  }))
})

const sharesValue = computed(() => props.shares)

// 动态生成快捷份额按钮，根据 maxShares 自适应
const quickShares = computed(() => {
  const max = props.maxShares
  if (max <= 0) return [1]
  if (max <= 5) return Array.from({ length: max }, (_, i) => i + 1)
  // 生成几个有意义的档位
  const steps = new Set([1, Math.floor(max / 4), Math.floor(max / 2), max])
  return [...steps].filter(v => v > 0 && v <= max).sort((a, b) => a - b)
})

const canUseMax = computed(() => props.maxShares > 0)

const updateTradeType = (value: 'buy' | 'sell') => {
  emit('update:tradeType', value)
}

const updateSelectedOutcome = (value: number | null) => {
  emit('update:selectedOutcomeId', value)
}

const updateShares = (value: number | null) => {
  const normalized = Math.max(1, Math.floor(Number(value || 1)))
  emit('update:shares', normalized)
}

// 处理快速选择
const handleQuickShare = (amount: number) => {
  if (amount <= props.maxShares) {
    emit('update:shares', amount)
  }
}

const handleUseMax = () => {
  if (!canUseMax.value) return
  emit('update:shares', props.maxShares)
}

// 防双击标记
const isSubmitting = ref(false)

// 执行交易
const executeTrade = async () => {
  if (!props.selectedOutcomeId || props.shares <= 0 || props.shares > props.maxShares || !props.quoteResult || props.quoteExceedsCash) return
  if (isSubmitting.value) return
  isSubmitting.value = true

  try {
    emit('executeTrade')
  } finally {
    // 延迟解锁，防止动画期间重复点击
    setTimeout(() => { isSubmitting.value = false }, 500)
  }
}
</script>

<template>
  <NCard title="交易面板" class="h-full">
    <NForm>
      <!-- 交易类型选择 -->
      <NFormItem label="交易类型">
        <NRadioGroup :value="props.tradeType" @update:value="updateTradeType">
          <NSpace>
            <NRadio value="buy">买入</NRadio>
            <NRadio value="sell" :disabled="!props.userHolding">卖出</NRadio>
          </NSpace>
        </NRadioGroup>
      </NFormItem>

      <!-- 选项选择 -->
      <NFormItem label="选择选项">
        <NSelect
          :value="props.selectedOutcomeId"
          @update:value="updateSelectedOutcome"
          :options="outcomeOptions"
          placeholder="请选择选项"
          :loading="loading"
        />
      </NFormItem>

      <!-- 份额输入 -->
      <NFormItem label="交易份额">
        <div class="space-y-3">
          <div class="flex items-center gap-2">
            <NInputNumber
              :value="sharesValue"
              @update:value="updateShares"
              :min="1"
              :max="Math.max(1, props.maxShares)"
              :step="1"
              placeholder="输入份额"
              class="w-full"
            />
            <NButton
              size="small"
              :type="props.shares === props.maxShares ? 'primary' : 'default'"
              :disabled="!canUseMax"
              @click="handleUseMax"
            >
              最大
            </NButton>
          </div>

          <div class="flex flex-wrap gap-2">
            <NButton
              v-for="amount in quickShares"
              :key="amount"
              size="small"
              :type="props.shares === amount ? 'primary' : 'default'"
              @click="handleQuickShare(amount)"
              :disabled="amount > props.maxShares"
            >
              {{ amount }}
            </NButton>
          </div>
          
          <div class="text-sm text-gray-500">
            最大可{{ props.tradeType === 'buy' ? '买入' : '卖出' }}: {{ props.maxShares }} 份额
            <span v-if="props.tradeType === 'buy'">(估算值，以报价为准)</span>
            <span v-if="props.userHolding && props.tradeType === 'sell'">
              (当前持仓: {{ props.userHolding.amount }} 份额)
            </span>
          </div>
        </div>
      </NFormItem>

      <!-- 报价明细 -->
      <div v-if="props.quoteResult" class="mb-4 p-3 border border-black text-sm space-y-1">
        <div class="flex justify-between">
          <span>均价:</span>
          <span class="font-medium">¥{{ props.quoteResult.avg_price?.toFixed(4) }}</span>
        </div>
        <div class="flex justify-between">
          <span>总额:</span>
          <span>¥{{ props.quoteResult.gross?.toFixed(2) }}</span>
        </div>
        <div class="flex justify-between" style="color:#888;">
          <span>手续费:</span>
          <span>¥{{ props.quoteResult.fee?.toFixed(2) }}</span>
        </div>
        <div class="flex justify-between font-bold border-t border-black pt-1">
          <span>{{ props.tradeType === 'buy' ? '应付' : '到手' }}:</span>
          <span>¥{{ props.quoteResult.net?.toFixed(2) }}</span>
        </div>
        <div v-if="props.quoteExceedsCash" class="mt-2 text-xs font-semibold" style="color: #c00;">
          现金不足：报价 ¥{{ props.quoteResult.net?.toFixed(2) }} 超出可用余额，请减少份额。
        </div>
      </div>

      <div class="mt-2">
        <NButton
          type="primary"
          size="large"
          :loading="marketStore.tradeLoading"
          :disabled="isSubmitting || !props.selectedOutcomeId || props.shares <= 0 || props.shares > props.maxShares || !props.quoteResult || props.quoteExceedsCash"
          @click="executeTrade"
          class="w-full"
        >
          {{ props.tradeType === 'buy' ? '买入' : '卖出' }} {{ props.shares }} 份额
        </NButton>
      </div>
    </NForm>

    <!-- 用户资产信息 -->
    <div v-if="authStore.isAuthenticated && userStore.summary" class="mt-4 rounded border border-black p-3">
      <h4 class="font-semibold mb-2">您的资产</h4>
      <div class="space-y-1.5 text-sm">
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
  </NCard>
</template>

<style scoped>
/* 组件样式 */
</style>