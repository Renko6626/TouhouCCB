<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { marketApi } from '@/api/market'
import { NButton, NEmpty } from 'naive-ui'
import type { OrderBookEntry } from '@/types/api'

const props = defineProps<{
  outcomeId: number
  marketId: number
  width?: string
  height?: string
  refreshToken?: number
}>()

const bidOrders = ref<OrderBookEntry[]>([])
const askOrders = ref<OrderBookEntry[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
let pollTimer: ReturnType<typeof setInterval> | null = null

const loadOrderBook = async () => {
  try {
    error.value = null
    const data = await marketApi.getOrderBook(props.marketId, props.outcomeId)
    bidOrders.value = [...(data.bids || [])].sort((a, b) => b.price - a.price)
    askOrders.value = [...(data.asks || [])].sort((a, b) => a.price - b.price)
  } catch (err) {
    // 后端暂不提供订单簿接口（LMSR 自动做市），静默处理
    bidOrders.value = []
    askOrders.value = []
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadOrderBook()
  pollTimer = setInterval(() => loadOrderBook(), 5000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})

watch(() => [props.outcomeId, props.marketId], () => {
  loading.value = true
  loadOrderBook()
})

// 外部触发刷新（如 SSE 事件后）
watch(() => props.refreshToken, () => {
  loadOrderBook()
})
</script>

<template>
  <div
    class="order-book-container"
    :style="{ width: props.width || '100%', height: props.height || '320px' }"
  >
    <!-- 加载状态 -->
    <div v-if="loading" class="ob-state">
      <span class="i-mdi-loading animate-spin" style="font-size:1.5rem"></span>
      <p class="mt-2 text-sm" style="color:#666">加载中...</p>
    </div>

    <!-- 空状态（LMSR 无传统订单簿） -->
    <div v-else-if="bidOrders.length === 0 && askOrders.length === 0" class="ob-state">
      <NEmpty description="此市场为 LMSR 自动做市，暂无传统订单簿数据">
        <template #extra>
          <p class="text-xs mt-1" style="color:#999">价格由算法根据供需自动调整</p>
        </template>
      </NEmpty>
    </div>

    <!-- 订单簿内容 -->
    <div v-else class="ob-content">
      <div class="ob-header">
        <span>订单簿</span>
        <span class="ob-count">{{ bidOrders.length }} 买 / {{ askOrders.length }} 卖</span>
      </div>

      <div class="ob-table-wrap">
        <table class="ob-table">
          <thead>
            <tr>
              <th class="ob-th">方向</th>
              <th class="ob-th ob-right">数量</th>
              <th class="ob-th ob-right">价格</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(order, i) in askOrders.slice(0, 8)" :key="`ask-${i}`" class="ob-row ob-ask">
              <td class="ob-td">卖</td>
              <td class="ob-td ob-right ob-mono">{{ order.shares.toLocaleString() }}</td>
              <td class="ob-td ob-right ob-mono ob-ask-price">{{ order.price.toFixed(4) }}</td>
            </tr>
            <tr class="ob-spread-row">
              <td colspan="3" class="ob-spread">
                <span>最佳买价 {{ bidOrders[0]?.price.toFixed(4) ?? '—' }}</span>
                <span>最佳卖价 {{ askOrders[0]?.price.toFixed(4) ?? '—' }}</span>
              </td>
            </tr>
            <tr v-for="(order, i) in bidOrders.slice(0, 8)" :key="`bid-${i}`" class="ob-row ob-bid">
              <td class="ob-td">买</td>
              <td class="ob-td ob-right ob-mono">{{ order.shares.toLocaleString() }}</td>
              <td class="ob-td ob-right ob-mono ob-bid-price">{{ order.price.toFixed(4) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.order-book-container {
  position: relative;
  overflow: hidden;
}

.ob-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
}

.ob-content {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.ob-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  border-bottom: 2px solid #000;
  background: #000;
  color: #fff;
}

.ob-count {
  font-size: 11px;
  font-weight: 400;
  color: rgba(255,255,255,0.6);
}

.ob-table-wrap {
  flex: 1;
  overflow-y: auto;
}

.ob-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.ob-th {
  padding: 5px 10px;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #888;
  border-bottom: 1px solid #e0e0e0;
  background: #f5f5f5;
}

.ob-td {
  padding: 4px 10px;
  border-bottom: 1px solid #f0f0f0;
}

.ob-right { text-align: right; }
.ob-mono { font-variant-numeric: tabular-nums; }

.ob-ask-price { color: #c00000; font-weight: 600; }
.ob-bid-price { color: #006600; font-weight: 600; }

.ob-spread-row td {
  padding: 4px 10px;
  background: #f5f5f5;
  border-top: 1px solid #ccc;
  border-bottom: 1px solid #ccc;
}

.ob-spread {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: #555;
}
</style>
