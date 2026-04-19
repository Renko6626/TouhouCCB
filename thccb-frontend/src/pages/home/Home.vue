<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useMarketStore } from '@/stores/market'
import { useAuthStore } from '@/stores/auth'
import { useUserStore } from '@/stores/user'
import { NButton } from 'naive-ui'
import MarketCard from '@/components/market/MarketCard.vue'

const router = useRouter()
const marketStore = useMarketStore()
const authStore = useAuthStore()
const userStore = useUserStore()

const loading = ref(false)
const summaryLoading = ref(false)

onMounted(async () => {
  const tasks: Promise<unknown>[] = []
  if (!marketStore.markets.length) {
    loading.value = true
    tasks.push(marketStore.fetchMarkets().finally(() => { loading.value = false }))
  }
  if (authStore.isAuthenticated && !userStore.summary) {
    summaryLoading.value = true
    tasks.push(userStore.fetchSummary().finally(() => { summaryLoading.value = false }))
  }
  await Promise.all(tasks)
})

const featuredMarkets = computed(() => marketStore.activeMarkets.slice(0, 4))

const stats = computed(() => [
  { label: '全部市场', value: marketStore.markets.length },
  { label: '交易中', value: marketStore.markets.filter(m => m.status === 'trading').length },
  { label: '已暂停', value: marketStore.markets.filter(m => m.status === 'halt').length },
  { label: '已结算', value: marketStore.markets.filter(m => m.status === 'settled').length },
])

// 盈亏相关
const pnl = computed(() => userStore.summary?.unrealized_pnl ?? 0)
const pnlDirection = computed<'up' | 'down' | 'flat'>(() => {
  if (pnl.value > 0) return 'up'
  if (pnl.value < 0) return 'down'
  return 'flat'
})
const pnlSign = computed(() => pnl.value > 0 ? '+' : pnl.value < 0 ? '−' : '')
const pnlAbs = computed(() => Math.abs(pnl.value))

const pnlPercent = computed(() => {
  const cost = userStore.summary?.total_cost_basis ?? 0
  if (cost <= 0) return null
  return (pnl.value / cost) * 100
})

const showPnlHero = computed(() => authStore.isAuthenticated && userStore.summary)
</script>

<template>
  <div class="home-page">

    <!-- ── 英雄区：登录后展示持仓浮盈；未登录展示品牌介绍 ── -->
    <section
      class="hero-section"
      :class="showPnlHero ? `hero-pnl hero-pnl-${pnlDirection}` : ''"
    >
      <div class="hero-inner">
        <!-- 登录后：盈亏视图 -->
        <template v-if="showPnlHero">
          <div class="hero-eyebrow">
            <span>浮动盈亏 · {{ userStore.summary!.rank }}</span>
          </div>
          <div class="pnl-number" :class="`pnl-${pnlDirection}`">
            <span class="pnl-sign">{{ pnlSign }}</span>¥{{ pnlAbs.toFixed(2) }}
          </div>
          <div v-if="pnlPercent !== null" class="pnl-percent" :class="`pnl-${pnlDirection}`">
            {{ pnlSign }}{{ Math.abs(pnlPercent).toFixed(2) }}%
            <span class="pnl-percent-base">基于 ¥{{ userStore.summary!.total_cost_basis.toFixed(2) }} 持仓成本</span>
          </div>
          <div v-else class="pnl-percent pnl-flat">
            暂无持仓，去市场建立你的第一笔仓位
          </div>

          <div class="pnl-stats">
            <div class="pnl-stat">
              <span class="pnl-stat-label">现金</span>
              <span class="pnl-stat-value">¥{{ userStore.summary!.cash.toFixed(2) }}</span>
            </div>
            <div class="pnl-stat">
              <span class="pnl-stat-label">持仓市值</span>
              <span class="pnl-stat-value">¥{{ userStore.summary!.holdings_value.toFixed(2) }}</span>
            </div>
            <div class="pnl-stat">
              <span class="pnl-stat-label">净资产</span>
              <span class="pnl-stat-value">¥{{ userStore.summary!.net_worth.toFixed(2) }}</span>
            </div>
          </div>

          <p class="pnl-note">* 仅含未结算持仓的浮动盈亏，不含已结算收益</p>

          <div class="hero-actions">
            <button class="hero-btn-primary" @click="router.push('/user/portfolio')">
              查看持仓
            </button>
            <button class="hero-btn-secondary" @click="router.push('/market/list')">
              浏览市场
            </button>
          </div>
        </template>

        <!-- 登录中的 loading 骨架 -->
        <template v-else-if="authStore.isAuthenticated && summaryLoading">
          <div class="hero-eyebrow">浮动盈亏</div>
          <div class="pnl-number pnl-skeleton">&nbsp;</div>
          <div class="pnl-percent-skeleton">&nbsp;</div>
        </template>

        <!-- 未登录：原品牌介绍 -->
        <template v-else>
          <div class="hero-eyebrow">预测市场 · 东方 Project</div>
          <h1 class="hero-title">东方炒炒币<br>预测市场</h1>
          <p class="hero-desc">
            基于 LMSR 算法的模拟预测市场交易平台。<br>
            交易您对幻想乡事件的判断，让市场发现真实概率。
          </p>
          <div class="hero-actions">
            <button class="hero-btn-primary" @click="router.push('/auth/register')">
              注册账号
            </button>
            <button class="hero-btn-secondary" @click="router.push('/auth/login')">
              立即登录
            </button>
          </div>
        </template>
      </div>
      <!-- 装饰角标 -->
      <div class="hero-corner hero-corner-tl"></div>
      <div class="hero-corner hero-corner-br"></div>
    </section>

    <!-- ── 统计栏（仅登录后显示） ── -->
    <section v-if="authStore.isAuthenticated" class="stats-bar">
      <div v-for="stat in stats" :key="stat.label" class="stat-item">
        <span class="stat-value">{{ stat.value }}</span>
        <span class="stat-label">{{ stat.label }}</span>
      </div>
    </section>

    <!-- ── 热门市场 ── -->
    <section class="section">
      <div class="section-header">
        <h2 class="section-title">热门市场</h2>
        <button class="section-more" @click="router.push('/market/list')">查看全部 →</button>
      </div>

      <div v-if="loading" class="loading-placeholder">
        <div v-for="i in 4" :key="i" class="skeleton-card"></div>
      </div>

      <div v-else-if="featuredMarkets.length" class="market-grid">
        <MarketCard
          v-for="market in featuredMarkets"
          :key="market.id"
          :market="market"
          @open="id => router.push(`/market/${id}/trade`)"
        />
      </div>

      <div v-else class="empty-markets">
        <p>暂无活跃市场</p>
        <NButton v-if="authStore.isAdmin" type="primary" @click="router.push('/admin/market-manage')">
          创建市场
        </NButton>
      </div>
    </section>

    <!-- ── 平台特色 ── -->
    <section class="section">
      <div class="section-header">
        <h2 class="section-title">平台特色</h2>
      </div>
      <div class="features-grid">
        <div class="feature-card feature-card-clickable" @click="router.push('/market/list')">
          <div class="feature-icon">
            <i class="i-mdi-chart-line"></i>
          </div>
          <h3 class="feature-title">LMSR 动态定价</h3>
          <p class="feature-desc">
            基于对数市场评分规则，价格随买卖动态调整，始终保证流动性。
          </p>
        </div>
        <div class="feature-card feature-card-clickable" @click="router.push('/market/list')">
          <div class="feature-icon">
            <i class="i-mdi-lightning-bolt"></i>
          </div>
          <h3 class="feature-title">实时数据推送</h3>
          <p class="feature-desc">
            SSE 实时流推送，成交瞬间刷新价格图表与持仓数据。
          </p>
        </div>
        <div class="feature-card feature-card-clickable" @click="router.push('/market/leaderboard')">
          <div class="feature-icon">
            <i class="i-mdi-trophy"></i>
          </div>
          <h3 class="feature-title">财富排行榜</h3>
          <p class="feature-desc">
            从「无名氏」到「大天狗的座上宾」，用净值竞逐幻想乡排名。
          </p>
        </div>
      </div>
    </section>

  </div>
</template>

<style scoped>
.home-page {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 40px;
}

/* ── 英雄区 ── */
.hero-section {
  position: relative;
  border: 4px solid #000000;
  background: #ffffff;
  padding: 56px 48px;
  overflow: hidden;
}

/* 工业框内层细线 */
.hero-section::before {
  content: '';
  position: absolute;
  inset: 5px;
  border: 1px solid #000000;
  pointer-events: none;
}

.hero-inner {
  position: relative;
  z-index: 1;
  max-width: 600px;
}

.hero-eyebrow {
  font-size: 12px;
  font-weight: 600;
  color: #666666;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.hero-eyebrow::before {
  content: '';
  display: inline-block;
  width: 24px;
  height: 2px;
  background: #000000;
}

.hero-title {
  font-size: clamp(32px, 5vw, 52px);
  font-weight: 900;
  color: #000000;
  line-height: 1.1;
  letter-spacing: -0.02em;
  margin-bottom: 20px;
}

.hero-desc {
  font-size: 15px;
  color: #444444;
  line-height: 1.7;
  margin-bottom: 32px;
}

.hero-actions {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.hero-btn-primary,
.hero-btn-secondary {
  padding: 12px 28px;
  font-size: 14px;
  font-weight: 700;
  border: 2px solid #000000;
  cursor: pointer;
  letter-spacing: 0.03em;
  transition: transform 0.1s, box-shadow 0.1s;
}

.hero-btn-primary {
  background: #000000;
  color: #ffffff;
  box-shadow: 4px 4px 0 #444444;
}

.hero-btn-primary:hover {
  transform: translate(-1px, -1px);
  box-shadow: 5px 5px 0 #444444;
}

.hero-btn-secondary {
  background: #ffffff;
  color: #000000;
  box-shadow: 4px 4px 0 #000000;
}

.hero-btn-secondary:hover {
  background: #f0f0f0;
  transform: translate(-1px, -1px);
  box-shadow: 5px 5px 0 #000000;
}

/* ── 盈亏英雄视图 ── */
.hero-pnl .hero-inner {
  max-width: 720px;
}

.hero-pnl-up {
  background: linear-gradient(180deg, var(--color-up-bg) 0%, #ffffff 60%);
}

.hero-pnl-down {
  background: linear-gradient(180deg, var(--color-down-bg) 0%, #ffffff 60%);
}

.pnl-number {
  font-size: clamp(44px, 9vw, 88px);
  font-weight: 900;
  line-height: 1;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  margin-bottom: 14px;
  color: #000;
}

.pnl-sign {
  margin-right: 4px;
}

.pnl-up { color: var(--color-up); }
.pnl-down { color: var(--color-down); }
.pnl-flat { color: #666; }

.pnl-percent {
  display: flex;
  align-items: baseline;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 20px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  margin-bottom: 24px;
}

.pnl-percent-base {
  font-size: 12px;
  font-weight: 500;
  color: #666;
  letter-spacing: 0.02em;
}

.pnl-stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0;
  border: 2px solid #000;
  background: #fff;
  margin-bottom: 16px;
}

.pnl-stat {
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  border-right: 1px solid #000;
}

.pnl-stat:last-child {
  border-right: none;
}

.pnl-stat-label {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #888;
}

.pnl-stat-value {
  font-size: 18px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  color: #000;
}

.pnl-note {
  font-size: 11px;
  color: #888;
  margin-bottom: 20px;
}

/* 骨架占位 */
.pnl-skeleton {
  width: 70%;
  height: clamp(44px, 9vw, 88px);
  background: linear-gradient(90deg, #eee 25%, #f5f5f5 50%, #eee 75%);
  background-size: 200% 100%;
  animation: skeleton-shimmer 1.4s infinite;
  margin-bottom: 14px;
  border-radius: 2px;
}

.pnl-percent-skeleton {
  width: 40%;
  height: 20px;
  background: linear-gradient(90deg, #eee 25%, #f5f5f5 50%, #eee 75%);
  background-size: 200% 100%;
  animation: skeleton-shimmer 1.4s infinite;
  border-radius: 2px;
}

/* 装饰角标 */
.hero-corner {
  position: absolute;
  width: 48px;
  height: 48px;
  pointer-events: none;
}

.hero-corner-tl {
  top: 14px;
  right: 14px;
  border-top: 3px solid #000000;
  border-right: 3px solid #000000;
}

.hero-corner-br {
  bottom: 14px;
  right: 14px;
  border-bottom: 3px solid #000000;
  border-right: 3px solid #000000;
}

/* ── 统计栏 ── */
.stats-bar {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  border: 2px solid #000000;
  background: #ffffff;
}

.stat-item {
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  border-right: 1px solid #000000;
}

.stat-item:last-child {
  border-right: none;
}

.stat-value {
  font-size: 28px;
  font-weight: 900;
  color: #000000;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}

.stat-label {
  font-size: 11px;
  color: #888888;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

/* ── 通用章节 ── */
.section {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.section-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  border-bottom: 2px solid #000000;
  padding-bottom: 10px;
}

.section-title {
  font-size: 18px;
  font-weight: 700;
  color: #000000;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.section-more {
  font-size: 12px;
  font-weight: 600;
  color: #000000;
  background: none;
  border: none;
  cursor: pointer;
  padding: 0;
  text-decoration: underline;
  text-underline-offset: 3px;
}

.section-more:hover {
  color: #333333;
}

/* 市场网格 */
.market-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}

/* 加载占位 */
.loading-placeholder {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}

.skeleton-card {
  height: 200px;
  border: 2px solid #e0e0e0;
  background: linear-gradient(90deg, #f5f5f5 25%, #ebebeb 50%, #f5f5f5 75%);
  background-size: 200% 100%;
  animation: skeleton-shimmer 1.4s infinite;
}

@keyframes skeleton-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* 空状态 */
.empty-markets {
  padding: 48px 0;
  text-align: center;
  border: 2px dashed #cccccc;
  color: #888888;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
}

/* ── 特色网格 ── */
.features-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 0;
  border: 2px solid #000000;
}

.feature-card {
  padding: 28px 24px;
  border-right: 1px solid #000000;
}

.feature-card:last-child {
  border-right: none;
}

.feature-icon {
  width: 40px;
  height: 40px;
  background: #000000;
  color: #ffffff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  margin-bottom: 14px;
}

.feature-title {
  font-size: 14px;
  font-weight: 700;
  color: #000000;
  margin-bottom: 8px;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.feature-desc {
  font-size: 13px;
  color: #555555;
  line-height: 1.6;
}

.feature-card-clickable {
  cursor: pointer;
  transition: background 0.15s;
}

.feature-card-clickable:hover {
  background: #f5f5f5;
}

/* 响应式 */
@media (max-width: 768px) {
  .hero-section {
    padding: 32px 24px;
  }
  /* 盈亏 hero 在手机上缩小辅助信息字号 */
  .pnl-percent {
    font-size: 17px;
    gap: 8px;
    margin-bottom: 20px;
  }
  .pnl-stats {
    grid-template-columns: 1fr;
  }
  .pnl-stat {
    border-right: none;
    border-bottom: 1px solid #000;
    flex-direction: row;
    justify-content: space-between;
    align-items: baseline;
    padding: 10px 14px;
  }
  .pnl-stat:last-child {
    border-bottom: none;
  }
  .pnl-stat-value {
    font-size: 16px;
  }
  .stats-bar {
    grid-template-columns: repeat(2, 1fr);
  }
  .stat-item:nth-child(2) {
    border-right: none;
  }
  .stat-item:nth-child(3),
  .stat-item:nth-child(4) {
    border-top: 1px solid #000000;
  }
  .market-grid,
  .loading-placeholder {
    grid-template-columns: 1fr;
  }
  .features-grid {
    grid-template-columns: 1fr;
  }
  .feature-card {
    border-right: none;
    border-bottom: 1px solid #000000;
  }
  .feature-card:last-child {
    border-bottom: none;
  }
}
</style>
