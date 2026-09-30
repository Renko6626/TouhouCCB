<script setup lang="ts">
import { computed, h, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NDataTable, NEmpty, NSelect, NSpin, NAlert, type DataTableColumns, type SelectOption } from 'naive-ui'
import type { Transaction } from '@/types/api'
import { useUserStore } from '@/stores/user'
import { redemptionApi } from '@/api/redemption'
import type { MyRedemptionItem } from '@/types/redemption'
import { fxApi, formatFxAmount } from '@/api/fx'
import type { FxPersonalTrade } from '@/types/fx'

defineOptions({ name: 'UserTransactions' })

const router = useRouter()
const userStore = useUserStore()

const loading = ref(false)
const loadError = ref('')
const fxError = ref('')
const fxTrades = ref<FxPersonalTrade[]>([])
const productFilter = ref<'all' | 'fx' | 'prediction'>('all')
const productOptions = [{ label: '全部产品', value: 'all' }, { label: 'FX 外汇', value: 'fx' }, { label: '预测市场', value: 'prediction' }]
const redemptionItems = ref<MyRedemptionItem[]>([])
const tradeTypeFilter = ref<'all' | 'buy' | 'sell' | 'settle' | 'liquidate'>('all')
const timeRangeFilter = ref<'all' | '7d' | '30d' | '90d'>('all')
const pageSize = ref<50 | 100 | 200>(100)

const tradeTypeOptions: SelectOption[] = [
  { label: '全部类型', value: 'all' },
  { label: '买入', value: 'buy' },
  { label: '卖出', value: 'sell' },
  { label: '结算', value: 'settle' },
  { label: '强制平仓', value: 'liquidate' },
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
  fxError.value = ''
  userStore.clearError()
  try {
    await Promise.all([
      userStore.fetchTransactions(pageSize.value),
      fxApi.getAllMyTrades(pageSize.value).then(rows => { fxTrades.value = rows }).catch(() => {
        fxError.value = 'FX 交易记录加载失败，请刷新重试。'
        fxTrades.value = []
      }),
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

const filteredFxTrades = computed(() => fxTrades.value.filter(row => {
  const type = row.is_liquidation ? 'liquidate' : row.side
  return (tradeTypeFilter.value === 'all' || tradeTypeFilter.value === type)
    && isWithinRange(row.created_at, timeRangeFilter.value)
}))
const resultCount = computed(() => (productFilter.value !== 'fx' ? filteredTransactions.value.length : 0)
  + (productFilter.value !== 'prediction' ? filteredFxTrades.value.length : 0))
const fxColumns: DataTableColumns<FxPersonalTrade> = [
  { title: '类型', key: 'side', render: row => row.is_liquidation ? '强制平仓' : row.side === 'buy' ? '买入' : '卖出' },
  { title: '币种', key: 'currency_code', render: row => h(NButton, { text: true, onClick: () => router.push({ path: '/fx', query: { pair: row.pair_id } }) }, { default: () => `${row.currency_name}（${row.currency_code}）` }) },
  { title: '外币数量', key: 'quantity', render: row => `${formatFxAmount(row.side === 'buy' ? row.output_amount : row.input_amount, 6)} ${row.currency_code}` },
  { title: '金圆券收支', key: 'gold', render: row => `${row.side === 'buy' ? '−' : '+'}${formatFxAmount(row.side === 'buy' ? row.input_amount : row.output_amount, 6)} 金` },
  { title: '手续费（已含）', key: 'fee_amount', render: row => `${formatFxAmount(row.fee_amount, 6)} ${row.side === 'buy' ? '金圆券' : row.currency_code}` },
  { title: '时间', key: 'created_at', render: row => new Date(row.created_at).toLocaleString('zh-CN') },
]

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
        liquidate: '强制平仓',
      }
      if (row.type === 'liquidate') {
        return h('span', { class: 'tx-type-liquidate' }, '强制平仓')
      }
      const style: Record<string, string> = {
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
    render: (row) =>
      h('span', { style: { fontVariantNumeric: 'tabular-nums' } }, row.shares.toLocaleString()),
  },
  {
    title: '单价',
    key: 'price',
    render: (row) =>
      h('span', { style: { fontVariantNumeric: 'tabular-nums' } }, `金 ${row.price.toFixed(4)}`),
  },
  {
    title: '金额',
    key: 'cost',
    render: (row) =>
      h('span', { style: { fontVariantNumeric: 'tabular-nums' } }, `金 ${row.cost.toFixed(2)}`),
  },
  {
    title: '时间',
    key: 'timestamp',
    width: 180,
    render: (row) =>
      h(
        'span',
        { style: { fontVariantNumeric: 'tabular-nums' } },
        new Date(row.timestamp).toLocaleString('zh-CN', {
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
        }),
      ),
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
        <NSelect v-model:value="productFilter" :options="productOptions" style="width: 140px" />
        <NSelect v-model:value="tradeTypeFilter" :options="tradeTypeOptions" style="width: 140px" />
        <NSelect v-model:value="timeRangeFilter" :options="timeRangeOptions" style="width: 140px" />
        <NSelect v-model:value="pageSize" :options="pageSizeOptions" :disabled="loading" style="width: 140px" />
      </div>
      <div class="toolbar-actions">
        <NButton @click="router.push('/user/portfolio')">← 我的资产</NButton>
        <NButton type="primary" :loading="loading" :disabled="loading" @click="loadTransactions">刷新</NButton>
      </div>
    </div>

    <!-- 结果数 -->
    <div class="result-count">
      共 <strong>{{ resultCount }}</strong> 条记录；每类产品最多读取最近 {{ pageSize }} 条，类型和时间筛选作用于已加载记录。
    </div>

    <section v-if="productFilter !== 'prediction'" class="tx-product">
      <h2>FX 外汇交易</h2>
      <p>买入数量为到账外币，卖出数量为扣除外币；金圆券收支已包含手续费影响。手续费按投入币种收取。</p>
      <NAlert v-if="fxError" type="error" :title="fxError" />
      <NDataTable v-else-if="filteredFxTrades.length" :columns="fxColumns" :data="filteredFxTrades" :loading="loading" :row-key="(row: FxPersonalTrade) => row.id" :scroll-x="1000" size="small" />
      <NSpin v-else-if="loading" />
      <NEmpty v-else description="暂无符合条件的 FX 成交" />
    </section>

    <section v-if="productFilter !== 'fx'" class="tx-product">
      <h2>预测市场交易</h2>
      <NAlert v-if="loadError && userStore.transactions.length" type="error" :title="loadError" />

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
      <NDataTable
        :columns="columns"
        :data="filteredTransactions"
        :loading="loading"
        :bordered="true"
        size="small"
        :scroll-x="900"
        :row-class-name="(row: Transaction) => row.type === 'liquidate' ? 'tx-liquidate-row' : ''"
      />
    </div>

    <!-- 空状态 -->
    <div v-else class="empty-state">
      <NEmpty description="暂无相关交易" />
    </div>
    </section>
  </div>
</template>

<style scoped>
.tx-product { margin-top: 24px; }
.tx-product h2 { font-size: 18px; font-weight: 700; margin-bottom: 12px; }
.tx-product p, .result-count { font-size: 12px; color: #555; margin: 12px 0; }
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

/* 强制平仓行 — 红底 */
:global(.tx-liquidate-row) {
  background: rgba(220, 38, 38, 0.08) !important;
}
:global(.tx-liquidate-row:hover) td {
  background: rgba(220, 38, 38, 0.13) !important;
}

/* 强制平仓 badge */
.tx-type-liquidate {
  display: inline-block;
  padding: 2px 8px;
  background: #dc2626;
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.5px;
  border: 1.5px solid #000;
}
</style>
