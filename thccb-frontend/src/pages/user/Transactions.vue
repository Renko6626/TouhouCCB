<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NCard, NDataTable, NEmpty, NSelect, NSpin, NTag, NAlert, type DataTableColumns, type SelectOption } from 'naive-ui'
import type { Transaction } from '@/types/api'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()

const loading = ref(false)
const loadError = ref('')
const tradeTypeFilter = ref<'all' | 'buy' | 'sell' | 'settle'>('all')
const timeRangeFilter = ref<'all' | '7d' | '30d' | '90d'>('all')

const tradeTypeOptions: SelectOption[] = [
  { label: '全部类型', value: 'all' },
  { label: '买入', value: 'buy' },
  { label: '卖出', value: 'sell' },
  { label: '结算', value: 'settle' },
]

const timeRangeOptions: SelectOption[] = [
  { label: '全部时间', value: 'all' },
  { label: '最近7天', value: '7d' },
  { label: '最近30天', value: '30d' },
  { label: '最近90天', value: '90d' },
]

const loadTransactions = async () => {
  loading.value = true
  loadError.value = ''
  userStore.clearError()
  try {
    await userStore.fetchTransactions()
    if (userStore.error) {
      loadError.value = userStore.error
    }
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : '加载失败，请重试'
  } finally {
    loading.value = false
  }
}

const isWithinRange = (timestamp: string, range: 'all' | '7d' | '30d' | '90d') => {
  if (range === 'all') {
    return true
  }

  const days = Number.parseInt(range.replace('d', ''), 10)
  const now = Date.now()
  const target = new Date(timestamp).getTime()
  return now - target <= days * 24 * 60 * 60 * 1000
}

const filteredTransactions = computed(() => {
  return userStore.transactions.filter((item) => {
    const typeMatch = tradeTypeFilter.value === 'all' || item.type === tradeTypeFilter.value
    const timeMatch = isWithinRange(item.timestamp, timeRangeFilter.value)
    return typeMatch && timeMatch
  })
})

const columns: DataTableColumns<Transaction> = [
  {
    title: '类型',
    key: 'type',
    width: 80,
    render: (row) => {
      const map: Record<string, string> = {
        buy: '买入',
        sell: '卖出',
        settle: '结算',
        settle_lose: '结算',
      }
      const style: Record<string, any> = {
        display: 'inline-block',
        padding: '1px 8px',
        fontSize: '12px',
        fontWeight: '600',
        letterSpacing: '0.04em',
        border: '1.5px solid #000',
      }
      if (row.type === 'buy') {
        Object.assign(style, { background: 'var(--color-up)', color: '#fff', borderColor: 'var(--color-up)' })
      } else if (row.type === 'sell') {
        Object.assign(style, { background: 'var(--color-down)', color: '#fff', borderColor: 'var(--color-down)' })
      } else {
        Object.assign(style, { background: '#000', color: '#fff' })
      }
      return h('span', { style }, map[row.type] ?? row.type)
    },
  },
  {
    title: '份额',
    key: 'shares',
    render: (row) => row.shares.toLocaleString(),
  },
  {
    title: '单价',
    key: 'price',
    render: (row) => `¥${row.price.toFixed(4)}`,
  },
  {
    title: '金额',
    key: 'cost',
    render: (row) => `¥${row.cost.toFixed(2)}`,
  },
  {
    title: '时间',
    key: 'timestamp',
    width: 180,
    render: (row) =>
      new Date(row.timestamp).toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      }),
  },
]

onMounted(async () => {
  await loadTransactions()
})
</script>

<template>
  <div class="transactions-page">
    <!-- 工具栏 -->
    <div class="filter-bar">
      <div class="toolbar-filters">
        <NSelect v-model:value="tradeTypeFilter" :options="tradeTypeOptions" style="width: 140px" />
        <NSelect v-model:value="timeRangeFilter" :options="timeRangeOptions" style="width: 140px" />
      </div>
      <div class="toolbar-actions">
        <NButton @click="router.push('/user/portfolio')">← 我的资产</NButton>
        <NButton type="primary" :loading="loading" @click="loadTransactions">刷新</NButton>
      </div>
    </div>

    <!-- 结果数 -->
    <div class="result-count">
      共 <strong>{{ filteredTransactions.length }}</strong> 条记录
    </div>

    <!-- 加载 -->
    <div v-if="loading && !userStore.transactions.length" class="text-center py-12">
      <NSpin size="large" />
      <p class="mt-3 text-black">加载交易记录中...</p>
    </div>

    <!-- 错误状态 -->
    <div v-else-if="loadError && !userStore.transactions.length" class="py-8">
      <NAlert type="error" :title="loadError">
        <div class="mt-2">
          <NButton size="small" @click="loadTransactions">重新加载</NButton>
        </div>
      </NAlert>
    </div>

    <!-- 表格 -->
    <div v-else-if="filteredTransactions.length > 0">
      <NDataTable :columns="columns" :data="filteredTransactions" :loading="loading" :bordered="true" size="small" />
    </div>

    <!-- 空状态 -->
    <div v-else class="empty-state">
      <NEmpty description="暂无符合条件的交易记录" />
    </div>
  </div>
</template>

<style scoped>
.transactions-page {
  max-width: 1100px;
  margin: 0 auto;
}

.toolbar-filters { display: flex; gap: 8px; flex-wrap: wrap; }
.toolbar-actions { display: flex; gap: 8px; }
</style>