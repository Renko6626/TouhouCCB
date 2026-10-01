<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useMarketStore } from '@/stores/market'
import { useAuthStore } from '@/stores/auth'
import { useUserStore } from '@/stores/user'
import { NButton } from 'naive-ui'
import MarketCard from '@/components/market/MarketCard.vue'
import Movers from '@/components/home/Movers.vue'
import RecentTrades from '@/components/home/RecentTrades.vue'
import MarginCallBanner from '@/components/market/MarginCallBanner.vue'
import RecentLiquidationsPanel from '@/components/home/RecentLiquidationsPanel.vue'
import FxOverview from '@/components/home/FxOverview.vue'
import ShortPositionPnl from '@/components/user/ShortPositionPnl.vue'
import { compareFxAmounts, divideFxAmount, formatFxAmount, subtractFxAmounts } from '@/api/fx'
import { fxHomeHoldings } from '@/utils/fxPresentation'

defineOptions({ name: 'FxHomePage' })

const router = useRouter()
const marketStore = useMarketStore()
const authStore = useAuthStore()
const userStore = useUserStore()

const loading = ref(false)
const summaryLoading = ref(false)
const showPrediction = ref(false)

onMounted(async () => {
  const tasks: Promise<unknown>[] = []
  loading.value = true
  tasks.push(marketStore.fetchMarkets().finally(() => { loading.value = false }))
  if (authStore.isAuthenticated) {
    summaryLoading.value = true
    tasks.push(userStore.fetchSummary().finally(() => { summaryLoading.value = false }))
  }
  await Promise.allSettled(tasks)
})

// 热门市场：按成交笔数降序取前 4，并列时按最后成交时间更近者优先。
// 依赖后端 /api/v1/market/list 的 trade_count / last_trade_at（iteration 2 新增）。
const featuredMarkets = computed(() =>
  [...marketStore.activeMarkets]
    .sort((a, b) => {
      const countDiff = (b.trade_count ?? 0) - (a.trade_count ?? 0)
      if (countDiff !== 0) return countDiff
      const aTs = a.last_trade_at ? new Date(a.last_trade_at).getTime() : 0
      const bTs = b.last_trade_at ? new Date(b.last_trade_at).getTime() : 0
      return bTs - aTs
    })
    .slice(0, 4)
)

// 盈亏相关
const holdings = computed(() => fxHomeHoldings(userStore.summary))
const pnl = computed(() => holdings.value.pnl)
const fxCost = computed(() => holdings.value.basis)
const pnlDirection = computed<'up' | 'down' | 'flat'>(() => {
  const direction = compareFxAmounts(pnl.value, '0')
  if (direction === 1) return 'up'
  if (direction === -1) return 'down'
  return 'flat'
})
const pnlSign = computed(() => pnlDirection.value === 'up' ? '+' : pnlDirection.value === 'down' ? '−' : '')
const pnlAbs = computed(() => pnl.value == null ? null
  : pnlDirection.value === 'down' ? subtractFxAmounts('0', pnl.value) : pnl.value)

const pnlPercent = computed(() => {
  if (pnl.value == null || fxCost.value == null || compareFxAmounts(fxCost.value, '0') !== 1) return null
  const ratio = divideFxAmount(pnl.value, fxCost.value)
  return ratio == null ? null : Number(ratio) * 100
})

const fxHoldings = computed(() => holdings.value.longHoldings)
const shortPositions = computed(() => holdings.value.shortPositions)
const hasFxHoldings = computed(() => fxHoldings.value.length > 0 || shortPositions.value.length > 0)

const showPnlHero = computed(() => authStore.isAuthenticated && userStore.summary)
</script>

<template>
  <div class="home-page">

    <!-- ── 保证金警告横幅（warning/danger 时可见） ── -->
    <MarginCallBanner />
    <p v-if="authStore.isAuthenticated && userStore.error" role="alert">账户信息刷新失败，请稍后刷新页面。已有数字为上次成功读取的快照。</p>

    <!-- ── 英雄区：登录后展示持仓浮盈；未登录展示品牌介绍 ── -->
    <section
      class="hero-section"
      :class="showPnlHero ? `hero-pnl hero-pnl-${pnlDirection}` : ''"
    >
      <div class="hero-inner">
        <!-- 登录后：盈亏视图 -->
        <template v-if="showPnlHero">
          <div class="hero-eyebrow">
            <span>FX战士 · 当前主玩法</span>
          </div>
          <h1 class="fx-home-title">我的外汇</h1>
          <p class="pnl-note">FX 多空持仓浮动盈亏</p>
          <div class="pnl-number" :class="`pnl-${pnlDirection}`">
            <template v-if="pnl !== null"><span class="pnl-sign">{{ pnlSign }}</span>金 {{ formatFxAmount(pnlAbs, 2) }}</template>
            <template v-else>估值待恢复</template>
          </div>
          <div v-if="pnlPercent !== null" class="pnl-percent" :class="`pnl-${pnlDirection}`">
            {{ pnlSign }}{{ Math.abs(pnlPercent).toFixed(2) }}%
            <span class="pnl-percent-base">基于 金 {{ formatFxAmount(fxCost, 2) }} {{ shortPositions.length ? '多头成本与空头开仓所得' : '持仓成本' }}</span>
          </div>
          <div v-else-if="!hasFxHoldings" class="pnl-percent pnl-flat">
            暂无外汇持仓，去行情页选择币种
          </div>
          <div v-else-if="pnl === null" class="pnl-percent pnl-flat">部分空头回补成本暂不可用，合计盈亏无法估算。</div>
          <p v-if="shortPositions.length" class="pnl-note">
            多头 {{ compareFxAmounts(holdings.longPnl, '0') === 1 ? '+' : '' }}金 {{ formatFxAmount(holdings.longPnl, 2) }}
            · 空头 {{ compareFxAmounts(holdings.shortPnl, '0') === 1 ? '+' : '' }}金 {{ formatFxAmount(holdings.shortPnl, 2) }}
          </p>

          <div class="pnl-stats">
            <div class="pnl-stat">
              <span class="pnl-stat-label">现金</span>
              <span class="pnl-stat-value">金 {{ userStore.summary!.cash.toFixed(2) }}</span>
            </div>
            <div class="pnl-stat">
              <span class="pnl-stat-label">外币市值</span>
              <span class="pnl-stat-value">金 {{ (userStore.summary?.fx_mtm ?? 0).toFixed(2) }}</span>
            </div>
            <div v-if="shortPositions.length" class="pnl-stat">
              <span class="pnl-stat-label">空头回补参考成本</span>
              <span class="pnl-stat-value">金 {{ formatFxAmount(userStore.summary?.short_cover_cost, 2) }}</span>
            </div>
            <div v-if="Number(userStore.summary!.debt) > 0" class="pnl-stat pnl-stat-debt">
              <span class="pnl-stat-label">负债</span>
              <span class="pnl-stat-value pnl-stat-debt-value">金 {{ Number(userStore.summary!.debt).toFixed(2) }}</span>
            </div>
            <div class="pnl-stat">
              <span class="pnl-stat-label">全账户净资产</span>
              <span class="pnl-stat-value">金 {{ userStore.netWorth?.toFixed(2) ?? '估值待恢复' }}</span>
            </div>
          </div>

          <p class="pnl-note">以上为最近账户快照，不含已实现收益；多头按账面市值估算，空头按剩余开仓所得减回补参考成本估算（含利息、手续费与滑点），实际以成交为准。全账户净资产包含预测市场持仓。</p>



          <div class="hero-actions">
            <button class="hero-btn-primary" @click="router.push('/fx')">进入外汇交易</button>
            <button class="hero-btn-secondary" @click="router.push('/user/portfolio')">我的资产</button>
            <button class="hero-btn-secondary" @click="router.push('/loan')">借款与风控规则</button>
          </div>
        </template>

        <!-- 登录中的 loading 骨架 -->
        <template v-else-if="authStore.isAuthenticated && summaryLoading">
          <div class="hero-eyebrow">正在读取外汇资产</div>
          <div class="pnl-number pnl-skeleton">&nbsp;</div>
          <div class="pnl-percent-skeleton">&nbsp;</div>
        </template>

        <!-- 未登录：原品牌介绍 -->
        <template v-else>
          <div class="hero-eyebrow">当前主玩法 · FX战士</div>
          <h1 class="hero-title">东方炒炒币<br>FX战士</h1>
          <p class="hero-desc">
            用金圆券买卖幻想外币，跟随汇率变化做出判断。<br>
            查看汇率、管理持仓，在模拟市场中体验交易。
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

    <section v-if="authStore.isAuthenticated && userStore.summary" class="holdings-section" aria-labelledby="holdings-title">
      <div class="section-header"><h2 id="holdings-title" class="section-title">我的外币持仓</h2><router-link to="/user/portfolio" class="section-more">全部资产 →</router-link></div>
      <div v-if="hasFxHoldings" class="holdings-strip">
        <router-link v-for="wallet in fxHoldings" :key="`long-${wallet.pair_id}`" :to="{ path: '/fx', query: { pair: wallet.pair_id } }" class="holding-item">
          <strong>{{ wallet.currency_name || wallet.currency_code }} <small>{{ wallet.currency_code }} · 多头</small></strong>
          <span>{{ formatFxAmount(wallet.foreign_amount) }} 外币</span>
          <span class="holding-value">估值 金 {{ formatFxAmount(wallet.mtm_gold, 2) }} <b>交易 →</b></span>
        </router-link>
        <router-link v-for="position in shortPositions" :key="`short-${position.pair_id}`" :to="{ path: '/fx', query: { pair: position.pair_id, action: 'cover' } }" class="holding-item">
          <strong>{{ position.currency_code }} <small>空头</small></strong>
          <span>含息欠币 {{ formatFxAmount(position.pending_short_debt) }} {{ position.currency_code }}</span>
          <ShortPositionPnl :proceeds-basis-gold="position.proceeds_basis_gold" :reference-cover-cost="position.reference_cover_cost" />
          <span class="holding-value">回补参考 金 {{ formatFxAmount(position.reference_cover_cost, 2) }} <b>回补 →</b></span>
        </router-link>
      </div>
      <p v-else class="holdings-empty">暂无外币持仓，从下方行情选择币种开始。</p>
    </section>

    <FxOverview />

    <section class="section">
      <RecentLiquidationsPanel />
    </section>

    <details class="prediction-section" @toggle="showPrediction = ($event.target as HTMLDetailsElement).open">
      <summary>预测市场 · 继续浏览其他玩法</summary>
      <div v-if="showPrediction" class="prediction-content">
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
        <p class="empty-title">博丽神社香火稀疏</p>
        <p class="empty-sub">当前没有活跃市场</p>
        <NButton v-if="authStore.isAdmin" type="primary" @click="router.push('/admin/markets')">
          创建市场
        </NButton>
      </div>
    </section>

    <!-- ── 涨跌榜（带时间窗口） ── -->
    <section class="section">
      <Movers />
    </section>

    <!-- ── 实时成交（轮询 5s） ── -->
    <section class="section">
      <RecentTrades />
    </section>

      </div>
    </details>

    <!-- ── 从这里开始（仅未登录用户展示，避免对老用户重复说明） ── -->
    <section v-if="!authStore.isAuthenticated" class="section">
      <div class="section-header">
        <h2 class="section-title">从这里开始</h2>
      </div>
      <div class="features-grid">
        <div class="feature-card feature-card-clickable" @click="router.push('/fx')">
          <div class="feature-icon">
            <i class="i-mdi-chart-line"></i>
          </div>
          <h3 class="feature-title">外汇交易</h3>
          <p class="feature-desc">
            查看各币种汇率与 K 线，按自己的判断买入或卖出。成交金额会影响价格。
          </p>
        </div>
        <div class="feature-card feature-card-clickable" @click="router.push('/loan')">
          <div class="feature-icon">
            <i class="i-mdi-lightning-bolt"></i>
          </div>
          <h3 class="feature-title">了解借款与强平</h3>
          <p class="feature-desc">
            可借额度和强平规则以当前生效配置为准。借款前先了解清算净值与风险。
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
.holdings-strip { display: flex; gap: 12px; overflow-x: auto; padding: 12px 2px 6px; }
.holding-item { flex: 0 0 230px; border: 2px solid #000; padding: 12px; display: flex; flex-direction: column; gap: 5px; color: #000; text-decoration: none; font-size: 13px; font-variant-numeric: tabular-nums; }
.holding-item:hover { background: #f5f5f5; }
.holding-item:focus-visible { outline: 3px solid #555; outline-offset: 2px; }
.holding-item small, .holding-value { color: #666; font-size: 11px; }
.holding-value { display: flex; justify-content: space-between; gap: 8px; }
.holding-value b { color: #000; white-space: nowrap; }
.holding-item :deep(.short-pnl) { padding: 6px 0; }
.holding-item :deep(.short-pnl-amount) { margin: 2px 0; font-size: 18px; }
.holding-item :deep(.short-pnl-note) { font-size: 11px; }
.holdings-empty { color: #666; font-size: 13px; margin: 12px 0 0; }
.hero-pnl .pnl-note { margin-bottom: 12px; }
.hero-pnl .pnl-percent { margin-bottom: 14px; }
.hero-pnl .hero-eyebrow { margin-bottom: 8px; }

.fx-home-title { font-size: 28px; font-weight: 800; margin: 0 0 8px; }
.prediction-section { border-top: 2px solid #000; padding-top: 16px; }
.prediction-section > summary { cursor: pointer; font-size: 16px; font-weight: 700; }
.prediction-content { display: flex; flex-direction: column; gap: 32px; padding-top: 24px; }

.home-page {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 24px;
}

/* ── 英雄区 ── */
.hero-section {
  position: relative;
  border: 4px solid #000000;
  background: #ffffff;
  padding: 24px 28px;
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
  max-width: 100%;
}

.hero-pnl-up {
  background: linear-gradient(180deg, var(--color-up-bg) 0%, #ffffff 60%);
}

.hero-pnl-down {
  background: linear-gradient(180deg, var(--color-down-bg) 0%, #ffffff 60%);
}

.pnl-number {
  font-size: clamp(32px, 5vw, 52px);
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
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
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

.pnl-stat-debt-value {
  color: var(--color-down);
}

.pnl-note {
  font-size: 11px;
  color: #888;
  margin-bottom: 20px;
}

/* 骨架占位 */
.pnl-skeleton {
  width: 70%;
  height: clamp(32px, 5vw, 52px);
  background: linear-gradient(90deg, #eee 25%, #f5f5f5 50%, #eee 75%);
  background-size: 200% 100%;
  animation: skeleton-shimmer 1.4s infinite;
  margin-bottom: 14px;
}

.pnl-percent-skeleton {
  width: 40%;
  height: 20px;
  background: linear-gradient(90deg, #eee 25%, #f5f5f5 50%, #eee 75%);
  background-size: 200% 100%;
  animation: skeleton-shimmer 1.4s infinite;
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
  padding: 48px 24px;
  text-align: center;
  border: 2px solid #cccccc;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}

.empty-markets .empty-title {
  font-size: 14px;
  font-weight: 700;
  color: #000000;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.empty-markets .empty-sub {
  font-size: 12px;
  color: #888888;
  margin-bottom: 10px;
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
    padding: 22px 18px;
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
