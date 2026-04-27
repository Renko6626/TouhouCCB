<script setup lang="ts">
import { computed, h, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NDataTable, NEmpty, NSelect, NSpin, NAlert, type DataTableColumns, type SelectOption } from 'naive-ui'
import type { Transaction } from '@/types/api'
import { useUserStore } from '@/stores/user'
import { redemptionApi } from '@/api/redemption'
import type { MyRedemptionItem } from '@/types/redemption'

const router = useRouter()
const userStore = useUserStore()

const loading = ref(false)
const loadError = ref('')
const redemptionItems = ref<MyRedemptionItem[]>([])
const tradeTypeFilter = ref<'all' | 'buy' | 'sell' | 'settle'>('all')
const timeRangeFilter = ref<'all' | '7d' | '30d' | '90d'>('all')
const pageSize = ref<50 | 100 | 200>(100)

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

const pageSizeOptions: SelectOption[] = [
  { label: '最近 50 条', value: 50 },
  { label: '最近 100 条', value: 100 },
  { label: '最近 200 条', value: 200 },
]

const loadTransactions = async () => {
  loading.value = true
  loadError.value = ''
  userStore.clearError()
  try {
    await userStore.fetchTransactions(pageSize.value)
    if (userStore.error) {
      loadError.value = userStore.error
    }
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : '加载失败，请重试'
  } finally {
    loading.value = false
  }
}

const loadRedemptions = async () => {
  try {
    redemptionItems.value = await redemptionApi.myRedemptions()
  } catch {
    // 兑换记录加载失败不阻塞主流程，悄悄略过
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
    title: '市场 / 选项',
    key: 'market',
    minWidth: 220,
    render: (row) => {
      const title = row.market_title ?? '—'
      const label = row.outcome_label ?? ''
      const marketEl = row.market_id
        ? h(
            'a',
            {
              href: `#/market/${row.market_id}/trade`,
              style: { color: '#000', textDecoration: 'underline', fontWeight: 600 },
              onClick: (e: MouseEvent) => {
                e.preventDefault()
                router.push(`/market/${row.market_id}/trade`)
              },
            },
            title,
          )
        : h('span', { style: { fontWeight: 600 } }, title)
      return h('div', { style: { display: 'flex', flexDirection: 'column', gap: '2px' } }, [
        marketEl,
        label
          ? h(
              'span',
              { style: { fontSize: '11px', color: '#555', letterSpacing: '0.04em' } },
              label,
            )
          : null,
      ])
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

// 切换条数时重新拉取（类型/时间筛选是纯前端过滤，不触发 refetch）
watch(pageSize, () => {
  loadTransactions()
})

onMounted(async () => {
  await loadTransactions()
  await loadRedemptions()
})

const redemptionTotal = computed(() =>
  redemptionItems.value.reduce((s, r) => s + Number(r.paid_amount), 0),
)
</script>

<template>
  <div class="transactions-page">
    <!-- 兑换购买摘要：跳转到「我的兑换」 -->
    <div v-if="redemptionItems.length > 0" class="redemption-summary">
      <div>
        <span class="summary-label">兑换购买</span>
        <strong class="summary-count">{{ redemptionItems.length }}</strong> 笔，
        累计支出 <strong>{{ redemptionTotal.toFixed(2) }}</strong>
      </div>
      <NButton size="small" @click="router.push('/my/redemptions')">查看详情 →</NButton>
    </div>

    <!-- 工具栏 -->
    <div class="filter-bar">
      <div class="toolbar-filters">
        <NSelect v-model:value="tradeTypeFilter" :options="tradeTypeOptions" style="width: 140px" />
        <NSelect v-model:value="timeRangeFilter" :options="timeRangeOptions" style="width: 140px" />
        <NSelect v-model:value="pageSize" :options="pageSizeOptions" style="width: 140px" />
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
      <NDataTable :columns="columns" :data="filteredTransactions" :loading="loading" :bordered="true" size="small" :scroll-x="900" />
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
.redemption-summary {
  display: flex; justify-content: space-between; align-items: center; gap: 12px;
  padding: 10px 14px; margin-bottom: 12px;
  border: 2px solid #000; background: #fff; box-shadow: 4px 4px 0 #000;
  font-size: 13px; flex-wrap: wrap;
}
.summary-label {
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.06em; margin-right: 8px;
}
.summary-count { font-size: 16px; }
</style>