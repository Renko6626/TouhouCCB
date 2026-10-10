<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'

// Throwaway UI prototype. Numbers deliberately model only fixed-price trades,
// not production AMM quotes, credit admission, interest, or liquidation.
type Direction = 'long' | 'short'
type Scenario = 'flat' | 'long' | 'short'
const route = useRoute()
const direction = ref<Direction>(route.query.direction === 'short' ? 'short' : 'long')
const scenario = ref<Scenario>(
  route.query.position === 'long' ? 'long' : route.query.position === 'short' ? 'short' : 'flat',
)
const price = 0.2
const feeRate = 0.002
const initialMargin = 1 / 19
const cash = ref(500)
const goldDebt = ref(0)
const longQuantity = ref(0)
const longCost = ref(0)
const shortQuantity = ref(0)
const restrictedGold = ref(0)
const shortBasis = ref(0)
const size = ref<number | ''>(1000)
const interval = ref('15 分钟')
const notice = ref('')
const closePercent = ref<number | null>(null)

const money = (n: number, digits = 2) =>
  n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
const signed = (n: number) => `${n >= 0 ? '+' : '−'}${money(Math.abs(n))}`
const longValue = computed(() => longQuantity.value * price)
const coverCost = computed(() => shortQuantity.value * price * (1 + feeRate))
const equity = computed(
  () => cash.value + longValue.value + restrictedGold.value - coverCost.value - goldDebt.value,
)
const exposure = computed(() => longValue.value + shortQuantity.value * price)
const amount = computed(() => Number(size.value) || 0)
const fee = computed(() => amount.value * feeRate)
const ownCash = computed(() => Math.min(cash.value, amount.value + fee.value))
const newBorrow = computed(() => Math.max(0, amount.value + fee.value - cash.value))
const postEquity = computed(() => equity.value - fee.value * (direction.value === 'short' ? 2 : 1))
const postRiskBasis = computed(() =>
  direction.value === 'long'
    ? goldDebt.value + newBorrow.value + coverCost.value
    : goldDebt.value + coverCost.value + amount.value * (1 + feeRate),
)
const margin = computed(() =>
  postRiskBasis.value > 0 ? postEquity.value / postRiskBasis.value : null,
)
const leverage = computed(() =>
  postEquity.value > 0 ? (exposure.value + amount.value) / postEquity.value : 0,
)
const atLimit = computed(() => margin.value !== null && margin.value < initialMargin)
const nearLimit = computed(() => margin.value !== null && margin.value < initialMargin * 2)
const oppositeHolding = computed(() =>
  direction.value === 'short' ? longQuantity.value > 0 : shortQuantity.value > 0,
)
const valid = computed(
  () =>
    Number.isFinite(amount.value) &&
    amount.value > 0 &&
    !atLimit.value &&
    !oppositeHolding.value &&
    equity.value > 0,
)
const riskLabel = computed(() =>
  atLimit.value ? '超过开仓门槛' : nearLimit.value ? '接近开仓门槛' : '健康',
)
const riskFill = computed(() => Math.min(100, Math.max(4, (leverage.value / 20) * 100)))
const hasHolding = computed(() => longQuantity.value > 0 || shortQuantity.value > 0)
const holdingDirection = computed<Direction>(() => (shortQuantity.value > 0 ? 'short' : 'long'))
const holdingQuantity = computed(() =>
  holdingDirection.value === 'long' ? longQuantity.value : shortQuantity.value,
)
const pnl = computed(() =>
  holdingDirection.value === 'long'
    ? longValue.value - longCost.value
    : shortBasis.value - coverCost.value,
)
const closeCost = computed(() =>
  closePercent.value === null ? 0 : (coverCost.value * closePercent.value) / 100,
)
const canClose = computed(
  () => holdingDirection.value === 'long' || closeCost.value <= restrictedGold.value + cash.value,
)
const returnPreview = computed(() => {
  if (closePercent.value === null) return 0
  const part = closePercent.value / 100
  if (holdingDirection.value === 'long') {
    const proceeds = longValue.value * part * (1 - feeRate)
    return Math.max(0, proceeds - goldDebt.value)
  }
  return Math.max(0, restrictedGold.value - closeCost.value)
})

function setLeverage(target: number) {
  // Targets approximate total account exposure / equity, never per-order margin.
  size.value = Math.max(0, Math.round(equity.value * target - exposure.value))
  notice.value = ''
}

function setDirection(next: Direction) {
  direction.value = next
  notice.value = ''
}

function resetAccount() {
  cash.value = 500
  goldDebt.value = 0
  longQuantity.value = 0
  longCost.value = 0
  shortQuantity.value = 0
  restrictedGold.value = 0
  shortBasis.value = 0
  closePercent.value = null
  notice.value = ''
  if (scenario.value === 'long') {
    cash.value = 0
    goldDebt.value = 500
    longQuantity.value = 5200
    longCost.value = 1000
    direction.value = 'long'
  } else if (scenario.value === 'short') {
    shortQuantity.value = 5000
    restrictedGold.value = 1040
    shortBasis.value = 1040
    direction.value = 'short'
  }
  size.value = 1000
}
resetAccount()

function executeDemo() {
  if (!valid.value) return
  const n = amount.value
  const tradeFee = fee.value
  const borrowed = newBorrow.value
  if (direction.value === 'long') {
    cash.value += borrowed - n - tradeFee
    goldDebt.value += borrowed
    longQuantity.value += n / price
    longCost.value += n + tradeFee
  } else {
    shortQuantity.value += n / price
    restrictedGold.value += n - tradeFee
    shortBasis.value += n - tradeFee
  }
  notice.value = `模拟${direction.value === 'long' ? '做多' : '做空'}已完成，持仓已更新。`
}

function closeDemo() {
  if (closePercent.value === null || !canClose.value) return
  const part = closePercent.value / 100
  if (holdingDirection.value === 'long') {
    const proceeds = longValue.value * part * (1 - feeRate)
    const repay = Math.min(goldDebt.value, proceeds)
    cash.value += proceeds - repay
    goldDebt.value -= repay
    longQuantity.value *= 1 - part
    longCost.value *= 1 - part
  } else {
    const cost = closeCost.value
    const lockedUse = Math.min(restrictedGold.value, cost)
    restrictedGold.value -= lockedUse
    cash.value -= cost - lockedUse
    shortQuantity.value *= 1 - part
    shortBasis.value *= 1 - part
    if (shortQuantity.value === 0) {
      cash.value += restrictedGold.value
      restrictedGold.value = 0
    }
  }
  notice.value = `模拟${part === 1 ? '全部平仓' : '减仓'}已完成。`
  closePercent.value = null
}

// Fixed sample candles. Changing the period only changes the sample chart.
const candles = computed(() =>
  Array.from({ length: 54 }, (_, i) => {
    const shift = interval.value === '1 小时' ? 1.4 : interval.value === '4 小时' ? 2.2 : 1
    const trend = 215 - i * 2.5 + Math.sin(i * 0.35) * 24 * shift + Math.sin(i * 1.7) * 8
    const open = trend + Math.sin(i * 2.1) * 9
    const close = trend + Math.cos(i * 1.8) * 11
    return {
      x: 29 + i * 11.5,
      open,
      close,
      high: Math.min(open, close) - 5 - (i % 7),
      low: Math.max(open, close) + 5 + (i % 9),
      up: close < open,
      volume: 13 + ((i * 17) % 34),
    }
  }),
)
</script>

<template>
  <div class="fx-prototype">
    <div class="preview-strip">
      <span><b>前端样稿</b> 示例数据，操作仅在此页面生效</span>
      <label
        >示例账户
        <select v-model="scenario" @change="resetAccount">
          <option value="flat">尚未持仓</option>
          <option value="long">持有多头</option>
          <option value="short">持有空头</option>
        </select>
      </label>
    </div>
    <header class="app-nav">
      <router-link to="/" class="brand">东方炒炒币<span>FX战士</span></router-link>
      <nav aria-label="主导航">
        <span class="nav-active">外汇交易</span
        ><router-link to="/user/portfolio">我的资产</router-link
        ><router-link to="/loan">借款与还款</router-link>
      </nav>
      <span class="demo-user">模拟账户</span>
    </header>

    <main class="workspace">
      <header class="market-header">
        <div class="market-identity">
          <span class="currency-mark" aria-hidden="true">M</span>
          <div>
            <h1>摩拉 <span>MORA</span></h1>
            <p>1 摩拉值多少金圆券</p>
          </div>
        </div>
        <div class="market-price">
          <strong>0.2000</strong><span class="up">↑ 2.40% <small>近 24 小时</small></span>
        </div>
        <div class="market-volume">
          <span>24 小时成交额</span><strong>128,640 <small>金圆券</small></strong>
        </div>
        <span class="sample-market">示例行情</span>
      </header>

      <div class="account-bar" aria-label="账户概览">
        <span>我的账户</span>
        <div>
          净值 <b>{{ money(equity) }}</b>
        </div>
        <div>
          可用现金 <b>{{ money(cash) }}</b>
        </div>
        <div>
          当前杠杆 <b>{{ equity > 0 ? money(exposure / equity, 2) : '—' }}x</b>
        </div>
        <small>金额单位：金圆券</small>
      </div>

      <div class="trading-grid">
        <div class="market-column">
          <section class="chart-section" aria-label="摩拉示例价格走势">
            <div class="section-toolbar">
              <h2>价格走势</h2>
              <div class="periods">
                <button
                  v-for="period in ['15 分钟', '1 小时', '4 小时']"
                  :key="period"
                  :aria-pressed="interval === period"
                  :class="{ selected: interval === period }"
                  @click="interval = period"
                >
                  {{ period }}
                </button>
              </div>
            </div>
            <div class="chart-legend">
              <span>开 <b>0.1992</b></span
              ><span>高 <b>0.2010</b></span
              ><span>低 <b>0.1988</b></span
              ><span>收 <b class="up">0.2000</b></span>
            </div>
            <svg
              class="price-chart"
              viewBox="0 0 740 358"
              role="img"
              aria-label="示例 K 线，摩拉价格总体上涨，当前价格 0.2000 金圆券"
            >
              <g v-for="(y, i) in [36, 100, 164, 228, 290]" :key="y" class="chart-grid">
                <line x1="12" :y1="y" x2="665" :y2="y" />
                <text x="678" :y="y + 4">
                  {{ ['0.2060', '0.2020', '0.1980', '0.1940', '0.1900'][i] }}
                </text>
              </g>
              <g v-for="c in candles" :key="c.x" :class="c.up ? 'candle-up' : 'candle-down'">
                <line :x1="c.x" :y1="c.high" :x2="c.x" :y2="c.low" />
                <rect
                  :x="c.x - 3.4"
                  :y="Math.min(c.open, c.close)"
                  width="6.8"
                  :height="Math.max(2, Math.abs(c.close - c.open))"
                />
                <rect
                  :x="c.x - 3.4"
                  :y="326 - c.volume"
                  width="6.8"
                  :height="c.volume"
                  class="volume-bar"
                />
              </g>
              <line x1="12" y1="132" x2="665" y2="132" class="current-line" />
              <rect x="674" y="120" width="63" height="24" class="price-label-bg" />
              <text x="681" y="136" class="price-label">0.2000</text>
              <g class="chart-times">
                <text x="20" y="350">09:00</text>
                <text x="180" y="350">12:00</text>
                <text x="340" y="350">15:00</text>
                <text x="500" y="350">18:00</text>
                <text x="610" y="350">21:00</text>
              </g>
            </svg>
            <div class="chart-footer">
              <span>价格上涨有利于多头，价格下跌有利于空头</span><span>金圆券 / 摩拉</span>
            </div>
          </section>

          <section class="positions-section" aria-labelledby="position-title">
            <div class="section-toolbar">
              <h2 id="position-title">
                我的持仓 <span>{{ hasHolding ? '1' : '0' }}</span>
              </h2>
              <span class="muted">卖出或买回归还，都从这里平仓</span>
            </div>
            <div v-if="hasHolding" class="position-content">
              <div class="position-heading">
                <div>
                  <span class="position-tag" :class="holdingDirection">{{
                    holdingDirection === 'long' ? '↑ 多头' : '↓ 空头'
                  }}</span
                  ><strong>摩拉</strong>
                </div>
                <div class="position-pnl">
                  <span>{{ holdingDirection === 'long' ? '账面盈亏' : '参考平仓盈亏' }}</span
                  ><strong :class="pnl >= 0 ? 'up' : 'down'"
                    >{{ signed(pnl) }} <small>金圆券</small></strong
                  >
                </div>
              </div>
              <div class="position-values">
                <div>
                  <span>持仓数量</span><b>{{ money(holdingQuantity, 0) }} 摩拉</b>
                </div>
                <div>
                  <span>{{ holdingDirection === 'long' ? '买入均价' : '卖出均价' }}</span
                  ><b>{{
                    money(
                      (holdingDirection === 'long' ? longCost : shortBasis) / holdingQuantity,
                      4,
                    )
                  }}</b>
                </div>
                <div>
                  <span>{{ holdingDirection === 'long' ? '待还金圆券' : '锁定卖出所得' }}</span
                  ><b>{{ money(holdingDirection === 'long' ? goldDebt : restrictedGold) }}</b>
                </div>
              </div>
              <div class="position-bottom">
                <span>{{
                  holdingDirection === 'long'
                    ? '卖出所得先还借款，余额成为可用现金。'
                    : '平仓时买回摩拉归还，结清后释放剩余锁金。'
                }}</span>
                <div>
                  <button class="secondary-button" @click="closePercent = 50">减仓 50%</button
                  ><button class="black-button" @click="closePercent = 100">全部平仓</button>
                </div>
              </div>
            </div>
            <div v-else class="empty-position">
              <span class="empty-icon" aria-hidden="true">↗ ↘</span>
              <div>
                <strong>还没有摩拉持仓</strong>
                <p>选择看涨或看跌，成交后在这里查看盈亏、平仓。</p>
              </div>
            </div>
          </section>

          <section class="news-section">
            <h2>市场消息</h2>
            <div>
              <time>14:20</time>
              <p>璃月港公布本周贸易计划，摩拉交易活跃。</p>
              <span>示例</span>
            </div>
            <div>
              <time>11:05</time>
              <p>商会调整货运安排，市场关注后续需求变化。</p>
              <span>示例</span>
            </div>
          </section>
        </div>

        <section class="order-panel" :class="direction" aria-labelledby="order-title">
          <div class="order-heading">
            <h2 id="order-title">开仓</h2>
            <span>交易摩拉</span>
          </div>
          <div class="direction-picker" role="group" aria-label="选择交易方向">
            <button
              :class="{ active: direction === 'long' }"
              :aria-pressed="direction === 'long'"
              @click="setDirection('long')"
            >
              <span class="direction-arrow">↗</span><strong>看涨做多</strong><small>Long</small>
            </button>
            <button
              :class="{ active: direction === 'short' }"
              :aria-pressed="direction === 'short'"
              @click="setDirection('short')"
            >
              <span class="direction-arrow">↘</span><strong>看跌做空</strong><small>Short</small>
            </button>
          </div>
          <p class="direction-help">
            {{
              direction === 'long' ? '买入摩拉，等待上涨后卖出。' : '借入摩拉卖出，等待下跌后买回。'
            }}
          </p>

          <div class="size-heading">
            <label for="order-size">交易规模</label><span>金圆券</span>
          </div>
          <div class="size-input">
            <input
              id="order-size"
              v-model="size"
              type="number"
              min="1"
              step="1"
              inputmode="decimal"
            /><span>≈ {{ money(amount / price, 0) }} 摩拉</span>
          </div>
          <div class="leverage-heading">
            <label for="leverage-range">交易后账户杠杆</label
            ><strong>{{ money(leverage, 2) }}<small>x</small></strong>
          </div>
          <input
            id="leverage-range"
            class="leverage-range"
            type="range"
            min="1"
            max="20"
            step="1"
            :value="Math.max(1, Math.min(20, Math.round(leverage)))"
            aria-label="目标账户杠杆倍数"
            @input="setLeverage(Number(($event.target as HTMLInputElement).value))"
          />
          <div class="leverage-presets">
            <button
              v-for="x in [1, 2, 5, 10]"
              :key="x"
              :class="{ chosen: Math.round(leverage) === x }"
              :aria-pressed="Math.round(leverage) === x"
              @click="setLeverage(x)"
            >
              {{ x }}x</button
            ><span>最高 20x</span>
          </div>

          <div v-if="direction === 'long'" class="funding-breakdown">
            <div>
              <span>使用现金</span><strong>{{ money(ownCash) }}</strong>
            </div>
            <b>+</b>
            <div>
              <span>自动借入</span><strong>{{ money(newBorrow) }}</strong>
            </div>
            <b>=</b>
            <div>
              <span>买入及费用</span><strong>{{ money(amount + fee) }}</strong>
            </div>
          </div>
          <div v-else class="short-funding">
            <div>
              <span>借入并卖出</span><strong>{{ money(amount / price, 0) }} 摩拉</strong>
            </div>
            <div>
              <span>卖出所得锁定</span><strong>{{ money(amount - fee) }} 金圆券</strong>
            </div>
            <p>锁定资金用于买回归还，不能花用。</p>
          </div>

          <div class="payoff-preview">
            <span>如果汇率变化 1%</span>
            <div>
              <span
                >上涨 1%
                <b :class="direction === 'long' ? 'up' : 'down'">{{
                  signed(amount * 0.01 * (direction === 'long' ? 1 : -1))
                }}</b></span
              ><span
                >下跌 1%
                <b :class="direction === 'short' ? 'up' : 'down'">{{
                  signed(amount * 0.01 * (direction === 'short' ? 1 : -1))
                }}</b></span
              >
            </div>
            <small>本笔价格盈亏示意，未扣费用与利息</small>
          </div>

          <div class="order-summary">
            <div>
              <span>本笔手续费</span><strong>{{ money(fee) }} 金圆券</strong>
            </div>
            <div>
              <span>成交后账户风险</span
              ><strong :class="{ down: atLimit || nearLimit }">{{ riskLabel }}</strong>
            </div>
            <div class="risk-meter" aria-hidden="true">
              <span
                :style="{ width: `${riskFill}%` }"
                :class="{ elevated: nearLimit || atLimit }"
              ></span>
            </div>
          </div>
          <p class="shared-risk">全账户共同担保，亏损可能影响其他持仓。</p>
          <p v-if="oppositeHolding" class="order-error" role="status">
            请先平掉当前{{ direction === 'short' ? '多头' : '空头' }}，再切换方向。
          </p>
          <p v-else-if="atLimit" class="order-error" role="status">
            成交后不满足开仓门槛，请降低交易规模。
          </p>
          <button class="submit-order" :disabled="!valid" @click="executeDemo">
            {{ direction === 'long' ? (newBorrow > 0 ? '借款并做多' : '确认做多') : '借币并做空'
            }}<span aria-hidden="true">{{ direction === 'long' ? '↗' : '↘' }}</span>
          </button>
          <p v-if="notice" class="trade-notice" role="status">{{ notice }}</p>
          <details class="trade-details">
            <summary>费用、利息与风险明细</summary>
            <dl>
              <div>
                <dt>参考成交价</dt>
                <dd>0.2000 金圆券 / 摩拉</dd>
              </div>
              <div>
                <dt>成交后保证金率</dt>
                <dd>{{ margin === null ? '无借款风险占用' : `${money(margin * 100)}%` }}</dd>
              </div>
              <div>
                <dt>开仓门槛</dt>
                <dd>≥ {{ money(initialMargin * 100) }}%</dd>
              </div>
            </dl>
            <p>杠杆按全账户参考敞口 / 净值展示，不为单笔交易隔离保证金。已有持仓会占用额度。</p>
            <p>
              借款或欠币会产生利息；保证金不足可能触发强平。本样稿使用固定价格，未模拟 AMM
              滑点、利息和实际风控。
            </p>
          </details>
        </section>
      </div>
      <footer class="prototype-footer">
        <span>FX战士</span><span>模拟报价仅用于查看页面交互</span
        ><button @click="resetAccount">重置示例账户</button>
      </footer>
    </main>

    <div
      v-if="closePercent !== null"
      class="close-backdrop"
      @click.self="closePercent = null"
      @keydown.esc="closePercent = null"
    >
      <section
        class="close-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="close-title"
        tabindex="-1"
      >
        <div class="section-toolbar">
          <h2 id="close-title">{{ closePercent === 100 ? '全部平仓' : '减仓 50%' }}</h2>
          <button
            class="dismiss-button"
            aria-label="关闭平仓预览"
            autofocus
            @click="closePercent = null"
          >
            ×
          </button>
        </div>
        <p>
          {{
            holdingDirection === 'long'
              ? '卖出摩拉，所得优先偿还金圆券借款。'
              : '买回摩拉归还，使用锁定所得和可用现金支付。'
          }}
        </p>
        <dl>
          <div>
            <dt>{{ holdingDirection === 'long' ? '卖出数量' : '买回归还' }}</dt>
            <dd>{{ money((holdingQuantity * closePercent) / 100, 0) }} 摩拉</dd>
          </div>
          <div v-if="holdingDirection === 'short'">
            <dt>预计买回成本</dt>
            <dd>{{ money(closeCost) }} 金圆券</dd>
          </div>
          <div>
            <dt>
              {{
                holdingDirection === 'long'
                  ? '预计现金增加'
                  : closePercent === 100
                    ? '预计释放锁金'
                    : '剩余锁定所得'
              }}
            </dt>
            <dd>{{ money(returnPreview) }} 金圆券</dd>
          </div>
        </dl>
        <p v-if="!canClose" class="order-error">可用现金和锁定资金不足，请减少回补数量。</p>
        <button class="black-button" :disabled="!canClose" @click="closeDemo">
          确认模拟{{ closePercent === 100 ? '平仓' : '减仓' }}
        </button>
      </section>
    </div>
  </div>
</template>

<style scoped>
.fx-prototype {
  --ink: #000;
  --muted: #666;
  --rule: #e5e5e5;
  --soft: #f5f5f5;
  background: #fff;
  min-height: 100vh;
  font-family: 'IBM Plex Sans', 'Noto Sans SC', 'Segoe UI', sans-serif;
  color: var(--ink);
  font-size: 13px;
}
.fx-prototype :is(button, input, select) {
  font: inherit;
}
.fx-prototype button {
  cursor: pointer;
}
.fx-prototype button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.fx-prototype :is(button, input, select, summary, a):focus-visible {
  outline: 2px solid #000;
  outline-offset: 4px;
}
.fx-prototype strong,
.fx-prototype b {
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.up {
  color: var(--color-up);
}
.down {
  color: var(--color-down);
}
.muted {
  color: var(--muted);
  font-size: 12px;
}
.preview-strip {
  padding: 8px 28px;
  background: var(--soft);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
  color: #555;
  border-bottom: 1px solid #ddd;
}
.preview-strip b {
  color: #000;
  margin-right: 12px;
}
.preview-strip label {
  display: flex;
  align-items: center;
  gap: 8px;
}
.preview-strip select {
  padding: 3px 8px;
  background: #fff;
  border: 1px solid #bbb;
}
.app-nav {
  height: 66px;
  padding: 0 28px;
  display: flex;
  align-items: center;
  gap: 48px;
  border-bottom: 2px solid #000;
}
.brand {
  font-size: 18px;
  font-weight: 850;
  white-space: nowrap;
}
.brand span {
  border-left: 1px solid #aaa;
  margin-left: 12px;
  padding-left: 12px;
  font-size: 12px;
  color: #666;
}
.app-nav nav {
  display: flex;
  gap: 28px;
  align-items: center;
  height: 100%;
}
.app-nav nav > * {
  height: 100%;
  display: flex;
  align-items: center;
  color: #666;
}
.app-nav nav .nav-active {
  color: #000;
  font-weight: 750;
  border-bottom: 3px solid #000;
  margin-bottom: -2px;
}
.demo-user {
  margin-left: auto;
  border: 1px solid #ddd;
  padding: 5px 10px;
}
.workspace {
  max-width: 1240px;
  margin: 0 auto;
  padding: 28px 28px 16px;
}
.market-header {
  display: flex;
  gap: 34px;
  align-items: center;
  padding-bottom: 24px;
}
.market-identity {
  display: flex;
  gap: 12px;
  align-items: center;
}
.currency-mark {
  display: grid;
  place-items: center;
  width: 42px;
  height: 42px;
  border: 2px solid #000;
  font-size: 27px;
  font-weight: 800;
}
.market-identity h1 {
  font-size: 23px;
  font-weight: 800;
  line-height: 1.3;
}
.market-identity h1 span {
  font-size: 12px;
  color: #777;
  margin-left: 8px;
}
.market-identity p {
  color: #666;
  font-size: 12px;
  margin-top: 4px;
}
.market-price {
  display: flex;
  flex-direction: column;
}
.market-price > strong {
  font-size: 32px;
  font-weight: 750;
  line-height: 1.25;
  letter-spacing: -0.5px;
}
.market-price > span {
  font-size: 12px;
  margin-top: 3px;
}
.market-price small {
  color: #777;
  margin-left: 8px;
}
.market-volume {
  margin-left: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.market-volume > span {
  color: #666;
  font-size: 12px;
}
.market-volume strong {
  font-size: 17px;
}
.market-volume small {
  color: #666;
  font-size: 11px;
}
.sample-market {
  padding: 4px 8px;
  background: #f5f5f5;
  color: #666;
  font-size: 11px;
}
.account-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 28px;
  padding: 12px 16px;
  background: #f7f7f7;
  border: 1px solid #ddd;
  margin-bottom: 22px;
}
.account-bar > span {
  font-weight: 650;
}
.account-bar div {
  color: #666;
}
.account-bar b {
  color: #000;
  margin-left: 8px;
}
.account-bar small {
  margin-left: auto;
  color: #888;
  font-size: 11px;
}
.trading-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 360px;
  gap: 28px;
  align-items: start;
}
.market-column {
  min-width: 0;
}
.section-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.section-toolbar h2,
.news-section h2 {
  font-size: 14px;
  font-weight: 750;
}
.section-toolbar h2 > span {
  margin-left: 7px;
  color: #777;
  font-size: 12px;
}
.periods {
  display: flex;
  gap: 4px;
}
.periods button {
  background: #fff;
  border: none;
  font-size: 11px;
  color: #777;
  padding: 5px 9px;
}
.periods button.selected {
  background: #000;
  color: #fff;
}
.chart-section {
  padding: 0 0 14px;
  border-bottom: 1px solid #ddd;
}
.chart-legend {
  display: flex;
  gap: 14px;
  font-size: 10px;
  color: #888;
  margin: 20px 0 4px;
}
.chart-legend b {
  margin-left: 3px;
  color: #444;
}
.chart-legend b.up {
  color: var(--color-up);
}
.price-chart {
  display: block;
  width: 100%;
  height: auto;
  margin: 6px 0 10px;
}
.chart-grid line {
  stroke: #eee;
  stroke-width: 1;
}
.chart-grid text,
.chart-times {
  fill: #888;
  font-size: 10px;
}
.candle-up {
  fill: var(--color-up);
  stroke: var(--color-up);
}
.candle-down {
  fill: var(--color-down);
  stroke: var(--color-down);
}
.volume-bar {
  opacity: 0.25;
  stroke: none;
}
.current-line {
  stroke: var(--color-up);
  stroke-width: 1;
  stroke-dasharray: 4 4;
}
.price-label-bg {
  fill: var(--color-up);
}
.price-label {
  fill: #fff;
  font-size: 11px;
  font-weight: 650;
}
.chart-footer {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  color: #888;
  font-size: 11px;
}
.positions-section {
  padding: 22px 0;
  border-bottom: 1px solid #ddd;
}
.empty-position {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 32px 16px 20px;
}
.empty-icon {
  font-size: 25px;
  color: #aaa;
  letter-spacing: 4px;
}
.empty-position p {
  font-size: 12px;
  margin-top: 5px;
  color: #888;
}
.position-content {
  padding-top: 20px;
}
.position-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.position-heading > div:first-child {
  display: flex;
  align-items: center;
  gap: 10px;
}
.position-tag {
  padding: 3px 7px;
  font-weight: 650;
  font-size: 11px;
}
.position-tag.long {
  color: var(--color-up);
  background: var(--color-up-bg);
}
.position-tag.short {
  color: var(--color-down);
  background: var(--color-down-bg);
}
.position-pnl {
  display: flex;
  flex-direction: column;
  align-items: end;
}
.position-pnl > span {
  color: #777;
  font-size: 11px;
}
.position-pnl > strong {
  font-size: 23px;
}
.position-pnl small {
  font-size: 11px;
}
.position-values {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  padding: 16px 0;
  gap: 12px;
}
.position-values > div {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.position-values span {
  font-size: 11px;
  color: #777;
}
.position-bottom {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.position-bottom > span {
  font-size: 11px;
  color: #888;
  max-width: 52%;
}
.position-bottom > div {
  display: flex;
  gap: 8px;
}
.secondary-button,
.black-button {
  padding: 7px 12px;
  border: 1px solid #000;
  font-weight: 650;
  background: #fff;
  font-size: 12px;
}
.black-button {
  background: #000;
  color: #fff;
}
.news-section {
  padding-top: 22px;
}
.news-section > div {
  display: flex;
  align-items: baseline;
  gap: 12px;
  padding-top: 13px;
  font-size: 12px;
}
.news-section time {
  color: #888;
  font-size: 11px;
}
.news-section p {
  color: #555;
}
.news-section > div > span {
  color: #aaa;
  font-size: 10px;
  margin-left: auto;
  flex-shrink: 0;
}
.order-panel {
  border: 1.5px solid #000;
  padding: 20px;
}
.order-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 18px;
}
.order-heading h2 {
  font-size: 17px;
  font-weight: 800;
}
.order-heading > span {
  font-size: 12px;
  color: #777;
}
.direction-picker {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.direction-picker button {
  border: 1px solid #ddd;
  background: #fff;
  text-align: left;
  padding: 12px;
  display: grid;
  grid-template-columns: 20px 1fr;
  column-gap: 5px;
  align-items: center;
}
.direction-picker button small {
  grid-column: 2;
  color: #888;
  font-size: 10px;
  margin-top: 2px;
}
.direction-picker button strong {
  font-size: 14px;
  white-space: nowrap;
}
.direction-arrow {
  font-size: 21px;
  font-weight: 750;
  align-self: start;
  grid-row: span 2;
  line-height: 1.3;
}
.long .direction-picker button:first-child.active {
  border: 1.5px solid var(--color-up);
  background: var(--color-up-bg);
  color: var(--color-up);
}
.short .direction-picker button:last-child.active {
  border: 1.5px solid var(--color-down);
  background: var(--color-down-bg);
  color: var(--color-down);
}
.direction-help {
  color: #777;
  font-size: 11px;
  margin-top: 9px;
  margin-bottom: 20px;
}
.size-heading {
  display: flex;
  justify-content: space-between;
  margin-bottom: 8px;
}
.size-heading label {
  font-weight: 650;
}
.size-heading > span {
  font-size: 11px;
  color: #888;
}
.size-input {
  border: 1px solid #bbb;
  padding: 10px 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.size-input input {
  width: 100%;
  min-width: 0;
  border: 0;
  background: none;
  font-size: 24px;
  font-weight: 650;
  outline-offset: 2px;
  -moz-appearance: textfield;
}
.size-input input::-webkit-inner-spin-button {
  appearance: none;
}
.size-input > span {
  font-size: 11px;
  color: #888;
  white-space: nowrap;
}
.leverage-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 20px;
}
.leverage-heading label {
  font-size: 12px;
  color: #555;
}
.leverage-heading strong {
  font-size: 23px;
  line-height: 1.3;
}
.leverage-heading small {
  font-size: 14px;
  margin-left: 2px;
}
.leverage-range {
  width: 100%;
  accent-color: #000;
  height: 22px;
  margin: 6px 0 7px;
}
.leverage-presets {
  display: flex;
  gap: 6px;
  align-items: center;
}
.leverage-presets button {
  flex: 1;
  border: 1px solid #ddd;
  background: #fff;
  color: #666;
  padding: 4px;
  font-size: 11px;
}
.leverage-presets button.chosen {
  border-color: #000;
  background: #000;
  color: #fff;
}
.leverage-presets > span {
  font-size: 10px;
  color: #999;
  margin-left: 3px;
  white-space: nowrap;
}
.funding-breakdown {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  padding: 13px 10px;
  background: #f5f5f5;
  margin-top: 18px;
}
.funding-breakdown > div {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.funding-breakdown span {
  color: #777;
  font-size: 10px;
}
.funding-breakdown strong {
  font-size: 15px;
}
.funding-breakdown > b {
  font-size: 12px;
  color: #999;
}
.short-funding {
  background: #f5f5f5;
  padding: 12px;
  margin-top: 18px;
}
.short-funding > div {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  padding-bottom: 6px;
}
.short-funding > div span {
  color: #666;
}
.short-funding p {
  font-size: 10px;
  color: #888;
}
.payoff-preview {
  padding: 15px 0;
  margin-top: 4px;
  border-bottom: 1px solid #eee;
}
.payoff-preview > span {
  font-size: 11px;
  color: #666;
}
.payoff-preview > div {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin: 8px 0 4px;
  font-size: 11px;
  color: #666;
}
.payoff-preview b {
  margin-left: 7px;
  font-size: 14px;
}
.payoff-preview > small {
  color: #999;
  font-size: 10px;
}
.order-summary {
  padding: 15px 0 0;
}
.order-summary > div:not(.risk-meter) {
  display: flex;
  justify-content: space-between;
  padding-bottom: 10px;
  font-size: 12px;
}
.order-summary > div > span {
  color: #666;
}
.risk-meter {
  height: 4px;
  background: #eee;
  overflow: hidden;
}
.risk-meter > span {
  display: block;
  height: 100%;
  background: #777;
}
.risk-meter > span.elevated {
  background: var(--color-down);
}
.shared-risk {
  color: #666;
  font-size: 11px;
  margin: 10px 0 17px;
  line-height: 1.5;
}
.submit-order {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  border: 0;
  padding: 13px 15px;
  background: #000;
  color: #fff;
  font-weight: 750;
  font-size: 14px;
}
.submit-order > span {
  font-size: 21px;
  line-height: 1;
}
.trade-details {
  padding-top: 13px;
  color: #777;
  font-size: 11px;
}
.trade-details summary {
  cursor: pointer;
}
.trade-details p {
  margin-top: 10px;
  line-height: 1.7;
}
.trade-details dl,
.close-dialog dl {
  margin-top: 12px;
}
.trade-details dl > div,
.close-dialog dl > div {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  padding: 5px 0;
}
.trade-details dd {
  color: #333;
  text-align: right;
}
.order-error {
  color: var(--color-down);
  font-size: 12px;
  margin-bottom: 12px;
}
.trade-notice {
  padding: 10px 0 0;
  font-size: 12px;
}
.prototype-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  border-top: 1px solid #eee;
  padding-top: 14px;
  margin-top: 28px;
  color: #aaa;
  font-size: 11px;
}
.prototype-footer button {
  border: 0;
  background: none;
  color: #777;
  font-size: 11px;
  text-decoration: underline;
}
.close-backdrop {
  position: fixed;
  inset: 0;
  background: #0006;
  display: grid;
  place-items: center;
  padding: 20px;
  z-index: 200;
}
.close-dialog {
  width: min(100%, 420px);
  background: #fff;
  border: 2px solid #000;
  padding: 22px;
}
.close-dialog > p {
  margin-top: 16px;
  color: #666;
  font-size: 12px;
}
.close-dialog > button {
  width: 100%;
  margin-top: 18px;
}
.dismiss-button {
  background: none;
  border: none;
  font-size: 22px !important;
}
.close-dialog dd {
  font-weight: 650;
}
@media (max-width: 960px) {
  .trading-grid {
    grid-template-columns: minmax(0, 1fr) 330px;
    gap: 20px;
  }
  .workspace {
    padding: 22px 20px 16px;
  }
  .order-panel {
    padding: 17px;
  }
  .position-bottom {
    flex-wrap: wrap;
  }
  .position-bottom > span {
    max-width: 100%;
  }
  .market-volume {
    display: none;
  }
  .sample-market {
    margin-left: auto;
  }
  .account-bar {
    gap: 18px;
  }
  .account-bar small {
    display: none;
  }
}
@media (max-width: 720px) {
  .preview-strip {
    padding: 8px 16px;
    font-size: 10px;
  }
  .preview-strip label {
    display: none;
  }
  .preview-strip b {
    margin-right: 6px;
  }
  .app-nav {
    padding: 0 16px;
    height: 54px;
    gap: 20px;
  }
  .brand {
    font-size: 16px;
  }
  .brand span,
  .demo-user,
  .app-nav nav a {
    display: none;
  }
  .app-nav nav {
    margin-left: auto;
    gap: 0;
    font-size: 12px;
  }
  .workspace {
    padding: 20px 16px 14px;
  }
  .market-header {
    flex-wrap: wrap;
    gap: 16px;
    padding-bottom: 18px;
  }
  .market-identity {
    gap: 10px;
  }
  .market-identity h1 {
    font-size: 21px;
  }
  .market-price {
    margin-left: auto;
    text-align: right;
  }
  .market-price > strong {
    font-size: 29px;
  }
  .market-price small {
    margin-left: 4px;
    font-size: 10px;
  }
  .sample-market {
    display: none;
  }
  .currency-mark {
    width: 37px;
    height: 37px;
    font-size: 22px;
  }
  .market-identity p {
    font-size: 10px;
  }
  .account-bar {
    padding: 10px 12px;
    gap: 8px 14px;
    font-size: 11px;
    margin-bottom: 18px;
  }
  .account-bar > span {
    display: none;
  }
  .account-bar div:last-of-type {
    margin-left: auto;
  }
  .account-bar b {
    margin-left: 4px;
  }
  .trading-grid {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  .order-panel {
    width: 100%;
    order: -1;
    padding: 18px;
  }
  .market-column {
    width: 100%;
  }
  .direction-picker button {
    padding: 12px 15px;
  }
  .direction-help {
    margin-bottom: 18px;
  }
  .leverage-presets button {
    padding: 7px;
  }
  .submit-order {
    min-height: 48px;
  }
  .price-chart {
    margin-top: 12px;
  }
  .chart-legend {
    gap: 10px;
  }
  .chart-footer > span:first-child {
    display: none;
  }
  .positions-section .muted {
    display: none;
  }
  .position-bottom button {
    min-height: 40px;
  }
  .prototype-footer > span:first-child {
    display: none;
  }
  .prototype-footer {
    margin-top: 24px;
  }
  .news-section p {
    font-size: 11px;
  }
  .news-section > div {
    gap: 8px;
  }
}
@media (prefers-reduced-motion: reduce) {
  * {
    scroll-behavior: auto;
  }
}
</style>
