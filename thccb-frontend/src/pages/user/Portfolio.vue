<script setup lang="ts">
import { ref, onMounted, computed, h } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { useMarketStore } from '@/stores/market'
import { useAuthStore } from '@/stores/auth'
import { userApi } from '@/api/user'
import { extractErrorMessage } from '@/utils/errors'
import {
  NButton, NCard, NTag,
  NSpace, NSpin, NDataTable, NEmpty, NAlert, useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import type { Holding } from '@/types/api'
import MarketStatus from '@/components/market/MarketStatus.vue'

const router = useRouter()
const userStore = useUserStore()
const marketStore = useMarketStore()
const authStore = useAuthStore()
const message = useMessage()

const loading = ref(false)
const loadError = ref('')

// 修改昵称弹窗状态
const editingName = ref(false)
const newUsername = ref('')
const savingName = ref(false)
const nameError = ref('')

function openNameEditor() {
  newUsername.value = authStore.user?.username ?? ''
  nameError.value = ''
  editingName.value = true
}

async function saveUsername() {
  const name = newUsername.value.trim()
  if (!name) {
    nameError.value = '昵称不能为空'
    return
  }
  if (name.length < 2 || name.length > 32) {
    nameError.value = '昵称长度需在 2-32 字符之间'
    return
  }
  savingName.value = true
  nameError.value = ''
  try {
    const resp = await userApi.updateUsername(name)
    authStore.patchUsername(resp.username)
    message.success(resp.changed ? '昵称已更新' : '昵称未变更')
    editingName.value = false
  } catch (e) {
    nameError.value = extractErrorMessage(e, '修改失败')
  } finally {
    savingName.value = false
  }
}

const loadData = async () => {
  loading.value = true
  loadError.value = ''
  userStore.clearError()
  try {
    await Promise.all([
      userStore.fetchSummary(),
      userStore.fetchHoldings(),
      marketStore.fetchMarkets({ include_halt: true, include_settled: true }),
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

onMounted(() => { loadData() })

const marketById = computed(() => {
  const map = new Map<number, any>()
  marketStore.markets.forEach(m => map.set(m.id, m))
  return map
})

// 浮盈亏颜色
const pnlColor = (val: number) => {
  if (val > 0) return 'var(--color-up)'
  if (val < 0) return 'var(--color-down)'
  return '#333'
}

const pnlSign = (val: number) => val > 0 ? '+' : ''

// 持仓表格列 — 直接用后端返回的 avg_price / current_price / unrealized_pnl
const holdingsColumns: DataTableColumns<Holding> = [
  { title: '选项', key: 'outcome_label', width: 140 },
  {
    title: '持仓',
    key: 'amount',
    width: 90,
    render: (row) => row.amount.toLocaleString(),
  },
  {
    title: '均价',
    key: 'avg_price',
    width: 100,
    render: (row) => `¥${row.avg_price.toFixed(4)}`,
  },
  {
    // 卖出均价 = market_value / amount，与"均价"对照可直观看出每份盈亏（含 LMSR 滑点）
    title: '卖出均价',
    key: 'sell_avg_price',
    width: 110,
    render: (row) => {
      const v = row.amount > 0 ? row.market_value / row.amount : 0
      return `¥${v.toFixed(4)}`
    },
  },
  {
    title: '成本',
    key: 'cost_basis',
    width: 100,
    render: (row) => `¥${row.cost_basis.toFixed(2)}`,
  },
  {
    title: '市值',
    key: 'market_value',
    width: 100,
    render: (row) => `¥${row.market_value.toFixed(2)}`,
  },
  {
    title: '浮盈亏',
    key: 'unrealized_pnl',
    width: 110,
    render: (row) => {
      const val = row.unrealized_pnl
      return h('span', {
        style: { color: pnlColor(val), fontWeight: '700', fontVariantNumeric: 'tabular-nums' },
      }, `${pnlSign(val)}¥${val.toFixed(2)}`)
    },
  },
  {
    title: '操作',
    key: 'actions',
    width: 130,
    render: (row) => {
      const market = marketById.value.get(row.market_id)
      const canTrade = market?.status === 'trading'
      return h(NSpace, { size: 4 }, {
        default: () => [
          h(NButton, { size: 'small', onClick: () => router.push(`/market/${row.market_id}/trade`) }, { default: () => '详情' }),
          canTrade && h(NButton, { size: 'small', type: 'primary', onClick: () => router.push(`/market/${row.market_id}/trade`) }, { default: () => '交易' }),
        ],
      })
    },
  },
]

// 按市场分组
const holdingsByMarketArray = computed(() => {
  const groupMap = userStore.holdingsByMarket
  return Array.from(groupMap.entries()).map(([mId, holdings]) => {
    const market = marketById.value.get(mId)
    const totalValue = holdings.reduce((sum, h) => sum + h.market_value, 0)
    const totalPnl = holdings.reduce((sum, h) => sum + h.unrealized_pnl, 0)
    return {
      market_id: mId,
      market_title: market?.title || holdings[0]?.market_title || '未知市场',
      market_status: market?.status || 'unknown',
      holdings,
      total_value: totalValue,
      total_pnl: totalPnl,
    }
  })
})
</script>

<template>
  <div class="portfolio-page">
    <!-- 加载状态 -->
    <div v-if="loading && !userStore.summary" class="loading-state">
      <NSpin size="large" />
      <p>正在加载…</p>
    </div>

    <!-- 错误状态 -->
    <div v-else-if="loadError && !userStore.summary" class="py-8">
      <NAlert type="error" :title="loadError">
        <div class="mt-2">
          <NButton size="small" @click="loadData">重新加载</NButton>
        </div>
      </NAlert>
    </div>

    <!-- 资产概览 -->
    <div v-else-if="userStore.summary">
      <div class="asset-grid">
        <div class="asset-card">
          <span class="asset-label">现金余额</span>
          <span class="asset-value">¥{{ userStore.summary.cash.toFixed(2) }}</span>
        </div>
        <div class="asset-card">
          <span class="asset-label">持仓成本</span>
          <span class="asset-value">¥{{ userStore.summary.total_cost_basis.toFixed(2) }}</span>
        </div>
        <div class="asset-card">
          <span class="asset-label">持仓市值</span>
          <span class="asset-value">¥{{ userStore.summary.holdings_value.toFixed(2) }}</span>
        </div>
        <div class="asset-card">
          <span class="asset-label">浮动盈亏</span>
          <span class="asset-value" :style="{ color: pnlColor(userStore.summary.unrealized_pnl) }">
            {{ pnlSign(userStore.summary.unrealized_pnl) }}¥{{ userStore.summary.unrealized_pnl.toFixed(2) }}
          </span>
        </div>
        <div
          v-if="Number(userStore.summary.debt) > 0"
          class="asset-card asset-card-debt"
          @click="router.push('/loan')"
          role="link"
          title="点击查看负债详情"
        >
          <span class="asset-label">负债</span>
          <span class="asset-value asset-value-debt">¥{{ Number(userStore.summary.debt).toFixed(2) }}</span>
        </div>
        <div class="asset-card asset-card-highlight asset-card-wide">
          <span class="asset-label">净资产</span>
          <span class="asset-value asset-value-net">¥{{ userStore.summary.net_worth.toFixed(2) }}</span>
        </div>
      </div>

      <!-- 账户 + 称号 + 快捷操作 -->
      <div class="rank-bar">
        <div class="rank-info">
          <span class="account-name">
            {{ authStore.user?.username || '匿名' }}
            <button type="button" class="account-edit-btn" @click="openNameEditor" title="修改昵称">✎</button>
          </span>
          <span class="rank-divider">·</span>
          <span class="rank-text">{{ userStore.summary.rank }}</span>
        </div>
        <div class="rank-actions">
          <NButton size="small" @click="router.push('/market/leaderboard')">排行榜</NButton>
          <NButton size="small" @click="router.push('/user/transactions')">交易记录</NButton>
        </div>
      </div>
    </div>

    <!-- 持仓详情 -->
    <div class="holdings-section">
      <div class="section-header">
        <h2 class="section-title">持仓明细</h2>
        <NButton :loading="loading" @click="loadData">刷新</NButton>
      </div>

      <div v-if="holdingsByMarketArray.length > 0" class="holdings-list">
        <NCard
          v-for="marketHoldings in holdingsByMarketArray"
          :key="marketHoldings.market_id"
          :title="marketHoldings.market_title"
        >
          <template #header-extra>
            <MarketStatus :status="marketHoldings.market_status" />
          </template>

          <NDataTable
            :columns="holdingsColumns"
            :data="marketHoldings.holdings"
            :bordered="false"
            :scroll-x="960"
            size="small"
          />

          <template #footer>
            <div class="card-footer-row">
              <div class="card-footer-stats">
                <span>市值：¥{{ marketHoldings.total_value.toFixed(2) }}</span>
                <span :style="{ color: pnlColor(marketHoldings.total_pnl), fontWeight: '700' }">
                  盈亏：{{ pnlSign(marketHoldings.total_pnl) }}¥{{ marketHoldings.total_pnl.toFixed(2) }}
                </span>
              </div>
              <NSpace size="small">
                <NButton size="small" @click="router.push(`/market/${marketHoldings.market_id}/trade`)">市场详情</NButton>
              </NSpace>
            </div>
          </template>
        </NCard>
      </div>

      <div v-else class="empty-state">
        <NEmpty description="暂无持仓">
          <template #extra>
            <NButton type="primary" @click="router.push('/market/list')">去市场看看</NButton>
          </template>
        </NEmpty>
      </div>
    </div>

    <!-- 修改昵称弹窗 -->
    <div v-if="editingName" class="name-modal-bg" @click.self="editingName = false">
      <div class="name-modal">
        <h3 class="name-modal-title">修改昵称</h3>
        <p class="name-modal-hint">2-32 字符；中文 / 英文 / 数字 / 下划线 / 连字符</p>
        <input
          v-model="newUsername"
          type="text"
          class="name-input"
          maxlength="32"
          :disabled="savingName"
          @keydown.enter="saveUsername"
          autofocus
        />
        <p v-if="nameError" class="name-error">
          <span class="warning-tag">注意</span>{{ nameError }}
        </p>
        <div class="name-modal-actions">
          <NButton @click="editingName = false" :disabled="savingName">取消</NButton>
          <NButton type="primary" @click="saveUsername" :loading="savingName">保存</NButton>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.portfolio-page {
  max-width: 1200px;
  margin: 0 auto;
}

.loading-state {
  text-align: center;
  padding: 64px 0;
  color: #333;
  font-size: 13px;
}

/* 资产数字网格 — 5 格 */
.asset-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  border: 2px solid #000;
  background: #fff;
}

.asset-card {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  border-right: 1px solid #000;
  border-bottom: 1px solid #000;
}

.asset-card:nth-child(4) {
  border-right: none;
}

.asset-card-wide {
  grid-column: 1 / -1;
  border-right: none;
  border-bottom: none;
}

.asset-card-highlight {
  background: #000;
}

.asset-label {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #888;
}

.asset-card-highlight .asset-label {
  color: rgba(255, 255, 255, 0.55);
}

.asset-value {
  font-size: 22px;
  font-weight: 800;
  color: #000;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}

.asset-value-net {
  color: #fff;
}

.asset-card-debt {
  cursor: pointer;
  transition: background 0.15s;
}
.asset-card-debt:hover {
  background: #fff0f0;
}
.asset-value-debt {
  color: var(--color-down);
}

/* 称号栏 */
.rank-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border: 2px solid #000;
  border-top: none;
  background: #f5f5f5;
}

.rank-info {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: #000;
  flex-wrap: wrap;
}

.account-name {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.account-edit-btn {
  appearance: none;
  background: transparent;
  border: 1.5px solid #000;
  width: 22px;
  height: 22px;
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
  font-family: inherit;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
}

.account-edit-btn:hover {
  background: #000;
  color: #fff;
}

.rank-divider {
  color: #999;
}

.rank-text {
  font-weight: 700;
}

.rank-actions {
  display: flex;
  gap: 8px;
}

/* 修改昵称弹窗 */
.name-modal-bg {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  z-index: 9000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px 16px;
}

.name-modal {
  width: 100%;
  max-width: 420px;
  background: #fff;
  border: 4px solid #000;
  box-shadow: 8px 8px 0 #000;
  padding: 20px 22px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.name-modal-title {
  font-size: 16px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.name-modal-hint {
  font-size: 12px;
  color: #666;
}

.name-input {
  padding: 8px 10px;
  border: 2px solid #000;
  font-size: 14px;
  font-family: inherit;
}

.name-input:focus {
  outline: none;
  background: #fafafa;
}

.name-error {
  font-size: 12px;
  color: #dc2626;
  margin: 0;
}

.warning-tag {
  display: inline-block;
  padding: 1px 6px;
  margin-right: 6px;
  background: #000;
  color: #fff;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.06em;
}

.name-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

/* 持仓区 */
.holdings-section {
  margin-top: 32px;
}

.holdings-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 2px solid #000;
  padding-bottom: 10px;
  margin-bottom: 16px;
}

.section-title {
  font-size: 15px;
  font-weight: 700;
  color: #000;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.card-footer-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
}

.card-footer-stats {
  display: flex;
  gap: 20px;
  font-variant-numeric: tabular-nums;
}

.empty-state {
  text-align: center;
  padding: 48px 0;
}

@media (max-width: 768px) {
  .asset-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  .asset-card:nth-child(2) {
    border-right: none;
  }
  .asset-card-wide {
    grid-column: 1 / -1;
  }
}
</style>
