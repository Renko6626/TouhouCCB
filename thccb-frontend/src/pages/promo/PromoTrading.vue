<script setup lang="ts">
import { computed, ref } from 'vue'
import PromoChart from './PromoChart.vue'
import {
  demoOutcomes,
  initialTrades,
  makeDemoCandles,
  movingAverage,
  type DemoInterval,
} from './data'

const selectedId = ref('reimu')
const selected = computed(() => demoOutcomes.find((item) => item.id === selectedId.value)!)
const interval = ref<DemoInterval>('5m')
const intervals: DemoInterval[] = ['1m', '5m', '15m', '1h']
const mode = ref<'candle' | 'line'>('candle')
const side = ref<'buy' | 'sell'>('buy')
const quantity = ref<number | ''>(1000)
const cash = ref(12860.5)
const positions = ref(demoOutcomes.map((item) => ({ ...item })))
const trades = ref(initialTrades.map((item) => ({ ...item })))
const notice = ref('')
const chart = ref<InstanceType<typeof PromoChart> | null>(null)
const candles = computed(() => makeDemoCandles(selected.value, interval.value))
const lastCandle = computed(() => candles.value[candles.value.length - 1]!)
const ma5 = computed(() => movingAverage(candles.value, 5).slice(-1)[0]!.value)
const ma20 = computed(() => movingAverage(candles.value, 20).slice(-1)[0]!.value)
const holding = computed(() => positions.value.find((item) => item.id === selectedId.value)!)
const heldPositions = computed(() => positions.value.filter((item) => item.shares > 0))
const positionValue = computed(() =>
  positions.value.reduce((sum, item) => sum + item.price * item.shares, 0),
)
const profit = computed(() =>
  positions.value.reduce((sum, item) => sum + (item.price - item.cost) * item.shares, 0),
)
const amount = computed(() => (Number(quantity.value) || 0) * selected.value.price)
const fee = computed(() => amount.value * 0.002)
const isValid = computed(
  () =>
    Number.isInteger(quantity.value) &&
    Number(quantity.value) > 0 &&
    (side.value === 'buy'
      ? amount.value + fee.value <= cash.value
      : Number(quantity.value) <= holding.value.shares),
)
const number = (value: number, digits = 2) =>
  value.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
const signed = (value: number) => `${value >= 0 ? '+' : ''}${number(value)}`

function executeDemoTrade() {
  if (!isValid.value) return
  const shares = Number(quantity.value)
  if (side.value === 'buy') {
    holding.value.cost =
      (holding.value.cost * holding.value.shares + amount.value + fee.value) /
      (holding.value.shares + shares)
    holding.value.shares += shares
    cash.value -= amount.value + fee.value
  } else {
    holding.value.shares -= shares
    cash.value += amount.value - fee.value
  }
  trades.value.unshift({
    time: '15:00:30',
    name: selected.value.name,
    side: side.value,
    price: selected.value.price,
    shares,
  })
  trades.value = trades.value.slice(0, 6)
  notice.value = `模拟${side.value === 'buy' ? '买入' : '卖出'}成功 · ${selected.value.name} ${number(shares, 0)} 份`
}

function reset() {
  selectedId.value = 'reimu'
  interval.value = '5m'
  mode.value = 'candle'
  side.value = 'buy'
  quantity.value = 1000
  cash.value = 12860.5
  positions.value = demoOutcomes.map((item) => ({ ...item }))
  trades.value = initialTrades.map((item) => ({ ...item }))
  notice.value = ''
}
</script>

<template>
  <div class="promo-page">
    <header class="terminal-header">
      <div class="brand">
        <span class="brand-mark">東</span>
        <div><strong>东方炒炒币</strong><span>TOUHOU CCB / FANTASY MARKET</span></div>
      </div>
      <nav aria-label="展示页导航">
        <span class="nav-active">交易工作台</span><a href="#positions">我的持仓</a
        ><a href="#market">角色行情</a>
      </nav>
      <div class="demo-user">
        <span class="demo-tag">DEMO</span><span>幻想乡交易员</span
        ><span class="user-avatar">幻</span>
      </div>
    </header>

    <div class="ticker-strip" aria-label="角色行情速览">
      <button v-for="item in demoOutcomes" :key="item.id" @click="selectedId = item.id">
        <span>{{ item.name }}</span
        ><strong>{{ item.price.toFixed(4) }}</strong
        ><span :class="item.change > 0 ? 'up' : 'down'"
          >{{ signed(item.change) }}% {{ item.change > 0 ? '↗' : '↘' }}</span
        >
      </button>
    </div>

    <section class="market-heading">
      <div>
        <div class="eyebrow">GENSOKYO EXCHANGE <span>/</span> 东方人气预测市场</div>
        <h1>为你的本命，<span class="heading-accent">看涨。</span></h1>
        <p>人气化作行情，让每一份热爱都有回响。</p>
      </div>
      <div class="heading-meta">
        <div class="market-open"><span></span> 市场交易中 <b>模拟行情</b></div>
        <p>2026.05.23 <span>15:00:30 CST</span></p>
        <div class="tools">
          <button @click="reset">↺ 重置演示</button
          ><button @click="chart?.download()">↓ 导出 K 线</button>
        </div>
      </div>
    </section>

    <section class="overview" aria-label="市场概况">
      <div>
        <span>市场累计成交额</span><strong>1,284,650<span class="unit"> 金圆券</span></strong>
      </div>
      <div>
        <span>参与交易人数</span><strong>2,386<span class="unit"> 人</span></strong>
      </div>
      <div>
        <span>今日成交笔数</span><strong>18,429<span class="unit"> 笔</span></strong>
      </div>
      <div>
        <span>你的持仓浮盈</span
        ><strong :class="profit >= 0 ? 'up' : 'down'"
          >{{ signed(profit) }}<span class="unit"> 金圆券</span></strong
        >
      </div>
    </section>

    <main class="workspace">
      <aside id="market" class="watchlist panel">
        <div class="panel-title">
          <h2>角色行情</h2>
          <span>05 / OUTCOMES</span>
        </div>
        <div class="watchlist-label"><span>角色 / 代码</span><span>价格 / 涨跌</span></div>
        <button
          v-for="(item, index) in demoOutcomes"
          :key="item.id"
          class="watchlist-item"
          :class="{ selected: selectedId === item.id }"
          :aria-pressed="selectedId === item.id"
          @click="((selectedId = item.id), (notice = ''))"
        >
          <span class="watchlist-name"
            ><span class="rank">0{{ index + 1 }}</span
            ><span
              ><strong>{{ item.name }}</strong
              ><small>{{ item.code }}</small></span
            ></span
          >
          <span class="watchlist-price"
            ><strong>{{ item.price.toFixed(4) }}</strong
            ><small :class="item.change > 0 ? 'up' : 'down'"
              >{{ signed(item.change) }}%</small
            ></span
          >
        </button>
        <div class="market-note">
          <span class="i-mdi-information-outline" aria-hidden="true"></span>
          价格由市场参与者的买卖共同决定，代表角色的相对人气。
        </div>
        <div class="account-box">
          <div class="eyebrow">MY ACCOUNT / 我的账户</div>
          <span>账户总资产</span><strong>{{ number(cash + positionValue) }}</strong>
          <div>
            <span>可用金圆券</span><b>{{ number(cash) }}</b>
          </div>
          <div>
            <span>持仓参考市值</span><b>{{ number(positionValue) }}</b>
          </div>
          <div class="account-footer">金圆券 · 虚拟游戏积分</div>
        </div>
      </aside>

      <div class="center-column">
        <section class="chart-panel panel">
          <div class="quote-heading">
            <div>
              <span class="eyebrow">{{ selected.code }} / {{ selected.title }}</span>
              <h2>{{ selected.name }} <span>人气份额</span></h2>
            </div>
            <div class="quote-price">
              <strong :class="selected.change > 0 ? 'up' : 'down'">{{
                selected.price.toFixed(4)
              }}</strong
              ><span :class="selected.change > 0 ? 'up' : 'down'"
                >{{ signed(selected.change) }}% <small>24H</small></span
              >
            </div>
          </div>
          <div class="chart-toolbar">
            <div class="chart-modes">
              <button :class="{ active: mode === 'candle' }" @click="mode = 'candle'">K 线图</button
              ><button :class="{ active: mode === 'line' }" @click="mode = 'line'">价格走势</button>
            </div>
            <div class="intervals">
              <button
                v-for="item in intervals"
                :key="item"
                :class="{ active: interval === item }"
                @click="interval = item"
              >
                {{ item }}
              </button>
            </div>
          </div>
          <div class="ohlc">
            <span
              >开 <b>{{ lastCandle.open.toFixed(4) }}</b></span
            ><span
              >高 <b>{{ lastCandle.high.toFixed(4) }}</b></span
            ><span
              >低 <b>{{ lastCandle.low.toFixed(4) }}</b></span
            ><span
              >收
              <b :class="lastCandle.close >= lastCandle.open ? 'up' : 'down'">{{
                lastCandle.close.toFixed(4)
              }}</b></span
            >
          </div>
          <div class="chart-legend">
            <span><i></i> MA5 {{ ma5.toFixed(4) }}</span
            ><span><i class="ma-slow"></i> MA20 {{ ma20.toFixed(4) }}</span
            ><span class="volume-label">VOL / 成交量</span>
          </div>
          <PromoChart ref="chart" :outcome="selected" :interval="interval" :mode="mode" />
          <div class="chart-footer">
            <span>100 根 K 线 · {{ interval }} 周期</span><span>LMSR 自动做市 / 模拟数据</span>
          </div>
        </section>

        <section id="positions" class="positions-panel panel">
          <div class="panel-title">
            <h2>
              我的持仓 <span class="count">{{ heldPositions.length }}</span>
            </h2>
            <span>PORTFOLIO</span>
          </div>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>角色</th>
                  <th>持有份额</th>
                  <th>平均成本</th>
                  <th>当前价格</th>
                  <th>持仓浮盈</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in heldPositions" :key="item.id" @click="selectedId = item.id">
                  <td>
                    <strong>{{ item.name }}</strong>
                  </td>
                  <td>{{ number(item.shares, 0) }}</td>
                  <td>{{ item.cost.toFixed(4) }}</td>
                  <td>{{ item.price.toFixed(4) }}</td>
                  <td :class="item.price >= item.cost ? 'up' : 'down'">
                    {{ signed((item.price - item.cost) * item.shares) }}
                  </td>
                </tr>
                <tr v-if="!heldPositions.length">
                  <td colspan="5" class="empty">暂无持仓，可以尝试模拟买入。</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <aside class="right-column">
        <section class="trade-panel panel">
          <div class="panel-title">
            <h2>下单交易</h2>
            <span class="demo-tag">模拟</span>
          </div>
          <div class="trade-body">
            <div class="trade-tabs">
              <button
                :class="{ 'buy-active': side === 'buy' }"
                @click="((side = 'buy'), (notice = ''))"
              >
                买入</button
              ><button
                :class="{ 'sell-active': side === 'sell' }"
                @click="((side = 'sell'), (notice = ''))"
              >
                卖出
              </button>
            </div>
            <label for="demo-outcome">交易角色</label
            ><select id="demo-outcome" v-model="selectedId">
              <option v-for="item in demoOutcomes" :key="item.id" :value="item.id">
                {{ item.name }} / {{ item.code }}
              </option>
            </select>
            <div class="quantity-label">
              <label for="demo-quantity">交易份额</label
              ><span>持有 {{ number(holding.shares, 0) }} 份</span>
            </div>
            <div class="quantity-input">
              <input
                id="demo-quantity"
                v-model.number="quantity"
                type="number"
                min="1"
                step="1"
                inputmode="numeric"
              /><span>份</span>
            </div>
            <div class="quick-amounts">
              <button
                v-for="value in [100, 500, 1000, 2000]"
                :key="value"
                :class="{ chosen: quantity === value }"
                @click="((quantity = value), (notice = ''))"
              >
                {{ number(value, 0) }}
              </button>
            </div>
            <div class="order-summary">
              <div>
                <span>当前参考价格</span><b>{{ selected.price.toFixed(4) }}</b>
              </div>
              <div>
                <span>参考成交金额</span><b>{{ number(amount) }}</b>
              </div>
              <div>
                <span>模拟手续费 <small>0.2%</small></span
                ><b>{{ number(fee) }}</b>
              </div>
              <div class="order-total">
                <span>{{ side === 'buy' ? '预计支付' : '预计获得' }}</span
                ><strong
                  >{{ number(side === 'buy' ? amount + fee : amount - fee) }}
                  <small>金</small></strong
                >
              </div>
            </div>
            <button
              class="execute-trade"
              :class="side === 'buy' ? 'buy-button' : 'sell-button'"
              :disabled="!isValid"
              @click="executeDemoTrade"
            >
              {{ side === 'buy' ? '买入' : '卖出' }} {{ selected.name }} <span>↗</span>
            </button>
            <p v-if="notice" class="trade-notice" role="status">{{ notice }}</p>
            <p v-else class="trade-hint">
              {{
                !isValid
                  ? '请输入有效份额，并确认余额或持仓充足。'
                  : '纯虚拟交易体验 · 所有操作仅在本页生效'
              }}
            </p>
          </div>
        </section>

        <section class="trades-panel panel">
          <div class="panel-title">
            <h2>最近成交</h2>
            <span class="live-label"><i></i> DEMO FEED</span>
          </div>
          <div class="trade-feed">
            <div v-for="(trade, index) in trades" :key="index" class="feed-row">
              <span class="feed-side" :class="trade.side === 'buy' ? 'up' : 'down'">{{
                trade.side === 'buy' ? '买' : '卖'
              }}</span>
              <div>
                <strong>{{ trade.name }}</strong
                ><small>{{ trade.time }} · {{ trade.price.toFixed(4) }}</small>
              </div>
              <b>{{ number(trade.shares, 0) }}<small> 份</small></b>
            </div>
          </div>
        </section>
      </aside>
    </main>

    <footer class="promo-footer">
      <strong>TOUHOU CCB<span>让热爱，进入市场。</span></strong>
      <p>东方 Project 同人虚拟交易游戏 · 本页行情、账户与成交均为模拟数据 · 无真实货币交易</p>
      <span>FAN-MADE / NON-COMMERCIAL</span>
    </footer>
  </div>
</template>

<style scoped>
.promo-page {
  background: #fff;
  color: #000;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
button,
input,
select {
  font: inherit;
  border-radius: 0;
}
button {
  cursor: pointer;
  transition: background 0.15s;
}
button:focus-visible,
a:focus-visible,
input:focus-visible,
select:focus-visible {
  outline: 2px solid #000;
  outline-offset: 3px;
}
strong,
b,
h1,
h2 {
  font-weight: 800;
}
.up {
  color: var(--color-up);
}
.down {
  color: var(--color-down);
}
.terminal-header {
  height: 70px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  background: #000;
  color: #fff;
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand-mark {
  font-size: 27px;
  font-weight: 900;
  border: 2px solid #fff;
  width: 40px;
  text-align: center;
  line-height: 40px;
}
.brand strong {
  display: block;
  font-size: 21px;
  letter-spacing: 2px;
}
.brand div > span {
  display: block;
  font-size: 9px;
  letter-spacing: 1.6px;
  color: #aaa;
}
nav {
  display: flex;
  align-self: stretch;
  gap: 30px;
  align-items: center;
}
nav a {
  color: #aaa;
  font-size: 12px;
}
nav a:hover {
  color: #fff;
}
.nav-active {
  height: 100%;
  display: flex;
  align-items: center;
  border-bottom: 3px solid #fff;
  font-weight: 700;
}
.demo-user {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
}
.demo-tag {
  border: 1px solid currentColor;
  padding: 2px 6px;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 1px;
}
.user-avatar {
  background: #fff;
  color: #000;
  padding: 4px 8px;
  font-weight: 800;
}
.ticker-strip {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  border: 2px solid #000;
  border-top: 0;
}
.ticker-strip button {
  display: flex;
  justify-content: center;
  gap: 10px;
  padding: 12px 6px;
  background: #fff;
  border: 0;
  border-right: 1px solid #ddd;
  font-size: 11px;
}
.ticker-strip button:last-child {
  border-right: 0;
}
.ticker-strip button:hover {
  background: #f5f5f5;
}
.market-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 28px 0 24px;
}
.eyebrow {
  font-size: 10px;
  letter-spacing: 1.4px;
  font-weight: 700;
  color: #777;
}
.eyebrow > span {
  margin: 0 8px;
  color: #bbb;
}
h1 {
  margin: 5px 0 4px;
  font-size: 40px;
  line-height: 1.25;
  letter-spacing: -1.5px;
}
.heading-accent {
  background: #000;
  color: #fff;
  font-weight: 900;
  padding: 0 8px 2px;
}
.market-heading p {
  color: #777;
  font-size: 12px;
}
.heading-meta {
  text-align: right;
}
.market-open {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  font-size: 12px;
  font-weight: 700;
}
.market-open > span {
  width: 6px;
  height: 6px;
  background: var(--color-up);
}
.market-open b {
  background: #f0f0f0;
  padding: 3px 6px;
  font-size: 10px;
  color: #777;
}
.heading-meta p {
  margin: 8px 0;
  font-size: 11px;
}
.heading-meta p > span {
  margin-left: 12px;
}
.tools {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
.tools button {
  background: #fff;
  border: 1px solid #ccc;
  padding: 4px 10px;
  font-size: 10px;
}
.tools button:hover {
  border-color: #000;
}
.overview {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  border: 2px solid #000;
  margin-bottom: 22px;
}
.overview > div {
  padding: 13px 20px;
  border-right: 1px solid #ddd;
}
.overview > div:last-child {
  border-right: 0;
}
.overview > div > span {
  display: block;
  font-size: 10px;
  color: #777;
  margin-bottom: 2px;
}
.overview strong {
  font-size: 23px;
  letter-spacing: -0.5px;
}
.overview .unit {
  font-size: 10px;
  font-weight: 400;
  color: #777;
  letter-spacing: 0;
}
.workspace {
  display: grid;
  grid-template-columns: 216px minmax(0, 1fr) 282px;
  gap: 18px;
  align-items: start;
}
.panel {
  border: 2px solid #000;
  background: #fff;
  box-shadow: 3px 3px 0 #000;
}
.panel-title {
  height: 45px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 15px;
  border-bottom: 1px solid #000;
}
.panel-title h2 {
  font-size: 13px;
}
.panel-title > span {
  color: #888;
  font-size: 8px;
  letter-spacing: 1px;
}
.watchlist-label {
  display: flex;
  justify-content: space-between;
  padding: 12px 12px 5px;
  font-size: 9px;
  color: #999;
}
.watchlist-item {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #fff;
  border: 0;
  border-bottom: 1px solid #eee;
  padding: 14px 12px;
  text-align: left;
}
.watchlist-item.selected {
  background: #f2f2f2;
  box-shadow: inset 3px 0 #000;
}
.watchlist-item:hover {
  background: #f5f5f5;
}
.watchlist-name {
  display: flex;
  gap: 9px;
  align-items: center;
}
.rank {
  color: #aaa;
  font: 10px monospace;
}
.watchlist-name strong {
  font-size: 12px;
}
.watchlist-item small {
  display: block;
  font-size: 9px;
  margin-top: 3px;
}
.watchlist-name small {
  color: #999;
  letter-spacing: 1px;
}
.watchlist-price {
  text-align: right;
}
.watchlist-price strong {
  font-size: 12px;
}
.market-note {
  display: flex;
  align-items: start;
  gap: 7px;
  padding: 15px 12px;
  font-size: 10px;
  line-height: 1.8;
  color: #888;
}
.market-note > span {
  flex-shrink: 0;
  margin-top: 3px;
}
.account-box {
  margin: 6px 10px 10px;
  padding: 16px 12px;
  background: #000;
  color: #fff;
}
.account-box .eyebrow {
  color: #aaa;
  font-size: 8px;
  letter-spacing: 1px;
  margin-bottom: 17px;
}
.account-box > span {
  font-size: 10px;
  color: #aaa;
}
.account-box > strong {
  display: block;
  font-size: 23px;
  margin: 2px 0 17px;
}
.account-box > div:not(.eyebrow):not(.account-footer) {
  display: flex;
  justify-content: space-between;
  margin-top: 8px;
  font-size: 10px;
}
.account-box div > span {
  color: #aaa;
}
.account-footer {
  border-top: 1px solid #333;
  padding-top: 12px;
  margin-top: 17px;
  font-size: 9px;
  color: #888;
}
.center-column,
.right-column {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-width: 0;
}
.quote-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 18px 12px;
}
.quote-heading .eyebrow {
  font-size: 9px;
  letter-spacing: 1px;
}
.quote-heading h2 {
  margin-top: 3px;
  font-size: 21px;
}
.quote-heading h2 > span {
  font-size: 10px;
  font-weight: 400;
  margin-left: 6px;
  color: #888;
}
.quote-price {
  text-align: right;
}
.quote-price > strong {
  font-size: 28px;
  line-height: 1.2;
  display: block;
  letter-spacing: -0.8px;
}
.quote-price > span {
  font-size: 11px;
  font-weight: 700;
}
.quote-price small {
  margin-left: 6px;
  color: #999;
  font-size: 9px;
}
.chart-toolbar {
  display: flex;
  justify-content: space-between;
  padding: 8px 16px;
  border-top: 1px solid #eee;
  border-bottom: 1px solid #eee;
}
.chart-modes,
.intervals {
  display: flex;
  gap: 4px;
}
.chart-toolbar button {
  padding: 4px 8px;
  background: #fff;
  border: 0;
  color: #888;
  font-size: 10px;
}
.chart-toolbar button.active {
  background: #000;
  color: #fff;
}
.chart-toolbar button:hover:not(.active) {
  background: #eee;
}
.ohlc {
  display: flex;
  gap: 14px;
  padding: 12px 16px 4px;
  font-size: 9px;
  color: #888;
}
.ohlc b {
  color: #444;
  margin-left: 3px;
  font-weight: 500;
}
.ohlc b.up {
  color: var(--color-up);
}
.ohlc b.down {
  color: var(--color-down);
}
.chart-legend {
  display: flex;
  gap: 16px;
  padding: 0 16px 7px;
  font-size: 9px;
  color: #777;
}
.chart-legend i {
  display: inline-block;
  width: 12px;
  border-top: 2px solid #111;
  vertical-align: middle;
  margin-right: 5px;
}
.chart-legend i.ma-slow {
  border-top: 2px dashed #888;
}
.volume-label {
  margin-left: auto;
  color: #aaa;
}
.chart-footer {
  display: flex;
  justify-content: space-between;
  border-top: 1px solid #ddd;
  padding: 7px 12px;
  font-size: 8px;
  color: #888;
}
.count {
  padding: 1px 4px;
  margin-left: 6px;
  background: #eee;
  color: #777;
  font-size: 10px;
}
.table-scroll {
  overflow-x: auto;
}
table {
  border-collapse: collapse;
  width: 100%;
  white-space: nowrap;
}
th {
  padding: 9px 12px;
  background: #f5f5f5;
  text-align: right;
  font-weight: 400;
  color: #888;
  font-size: 9px;
}
td {
  padding: 12px;
  text-align: right;
  border-top: 1px solid #eee;
  font-size: 10px;
}
th:first-child,
td:first-child {
  text-align: left;
}
tbody tr {
  cursor: pointer;
}
tbody tr:hover {
  background: #fafafa;
}
td.up,
td.down {
  font-weight: 700;
}
.empty {
  text-align: center;
  color: #888;
}
.trade-body {
  padding: 14px;
}
.trade-tabs {
  display: flex;
  margin-bottom: 17px;
}
.trade-tabs button {
  width: 50%;
  border: 1px solid #ddd;
  background: #f5f5f5;
  padding: 8px;
  color: #888;
  font-size: 12px;
  font-weight: 700;
}
.trade-tabs .buy-active {
  color: var(--color-up);
  background: var(--color-up-bg);
  border-color: var(--color-up);
}
.trade-tabs .sell-active {
  color: var(--color-down);
  background: var(--color-down-bg);
  border-color: var(--color-down);
}
label {
  display: block;
  font-size: 10px;
  font-weight: 700;
  margin-bottom: 7px;
}
select {
  width: 100%;
  border: 1px solid #ccc;
  background: #fff;
  padding: 9px;
  font-size: 11px;
  margin-bottom: 15px;
}
.quantity-label {
  display: flex;
  justify-content: space-between;
}
.quantity-label > span {
  font-size: 9px;
  color: #888;
}
.quantity-input {
  display: flex;
  border: 1px solid #000;
  align-items: center;
}
.quantity-input input {
  width: 100%;
  border: 0;
  padding: 9px 11px;
  min-width: 0;
  font-size: 17px;
  font-weight: 700;
}
.quantity-input > span {
  color: #888;
  margin-right: 12px;
  font-size: 10px;
}
.quick-amounts {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 5px;
  margin: 8px 0 17px;
}
.quick-amounts button {
  border: 1px solid #ddd;
  background: #fff;
  padding: 4px;
  font-size: 9px;
  color: #666;
}
.quick-amounts .chosen {
  border-color: #000;
  color: #000;
  background: #f5f5f5;
}
.order-summary {
  border-top: 1px dashed #ccc;
  padding-top: 13px;
}
.order-summary > div {
  display: flex;
  justify-content: space-between;
  margin-bottom: 10px;
  font-size: 10px;
}
.order-summary span {
  color: #888;
}
.order-summary small {
  color: #aaa;
  font-size: 8px;
}
.order-summary b {
  font-weight: 500;
}
.order-summary .order-total {
  align-items: center;
  border-top: 1px solid #ddd;
  padding-top: 12px;
  margin-top: 12px;
  margin-bottom: 15px;
}
.order-total strong {
  font-size: 23px;
  letter-spacing: -0.5px;
}
.order-total strong small {
  font-size: 10px;
  color: #777;
}
.execute-trade {
  width: 100%;
  border: 0;
  color: #fff;
  padding: 12px;
  font-size: 12px;
  font-weight: 800;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.buy-button {
  background: var(--color-up);
}
.sell-button {
  background: var(--color-down);
}
.execute-trade:hover {
  filter: brightness(0.92);
}
.execute-trade:disabled {
  background: #ddd;
  color: #888;
  cursor: not-allowed;
}
.trade-hint,
.trade-notice {
  font-size: 8px;
  margin-top: 10px;
  line-height: 1.7;
  color: #999;
}
.trade-notice {
  color: var(--color-up);
}
.live-label {
  display: flex;
  align-items: center;
  gap: 5px;
}
.live-label i {
  width: 4px;
  height: 4px;
  background: var(--color-up);
}
.trade-feed {
  padding: 2px 13px;
}
.feed-row {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 9px 0;
  border-bottom: 1px solid #eee;
}
.feed-row:last-child {
  border: 0;
}
.feed-side {
  font-size: 10px;
  font-weight: 800;
  padding: 2px 4px;
  border: 1px solid #ddd;
}
.feed-row div {
  flex: 1;
}
.feed-row div strong {
  display: block;
  font-size: 10px;
  font-weight: 600;
}
.feed-row small {
  display: block;
  color: #aaa;
  font-size: 8px;
}
.feed-row > b {
  font-size: 10px;
  font-weight: 500;
}
.feed-row > b small {
  display: inline;
}
.promo-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  margin-top: 27px;
  border-top: 2px solid #000;
  padding: 16px 0 4px;
}
.promo-footer > strong {
  font-size: 12px;
  letter-spacing: 1px;
  white-space: nowrap;
}
.promo-footer strong > span {
  display: block;
  font-size: 9px;
  letter-spacing: 1px;
  color: #888;
  font-weight: 400;
  margin-top: 3px;
}
.promo-footer p {
  font-size: 9px;
  color: #888;
}
.promo-footer > span {
  font-size: 8px;
  color: #aaa;
  letter-spacing: 0.5px;
}
@media (max-width: 1200px) {
  .workspace {
    grid-template-columns: 180px minmax(0, 1fr) 260px;
    gap: 14px;
  }
  .watchlist-item {
    padding: 14px 9px;
  }
  .rank {
    display: none;
  }
  .ticker-strip button {
    gap: 6px;
    font-size: 10px;
  }
  .quote-heading {
    padding: 16px 12px 12px;
  }
  .quote-heading h2 {
    font-size: 18px;
  }
  .quote-heading h2 > span {
    display: none;
  }
  nav {
    gap: 20px;
  }
}
@media (max-width: 1000px) {
  .workspace {
    grid-template-columns: minmax(0, 1fr) 260px;
  }
  .watchlist {
    grid-column: 1 / -1;
    display: grid;
    grid-template-columns: repeat(5, 1fr);
  }
  .watchlist .panel-title {
    grid-column: 1 / -1;
  }
  .watchlist-label,
  .market-note,
  .account-box {
    display: none;
  }
  .watchlist-name {
    display: block;
  }
  .watchlist-name strong {
    font-size: 10px;
  }
  .watchlist-name small {
    font-size: 8px;
  }
  .watchlist-item {
    border-right: 1px solid #eee;
  }
  .watchlist-price strong {
    font-size: 10px;
  }
  .watchlist-price small {
    font-size: 8px;
  }
  .demo-user > span:not(.demo-tag) {
    display: none;
  }
  .ticker-strip button {
    flex-wrap: wrap;
    gap: 3px 7px;
  }
  .ticker-strip button > span:first-child {
    width: 100%;
  }
  nav {
    gap: 15px;
  }
  .overview > div {
    padding: 12px;
  }
  .overview strong {
    font-size: 19px;
  }
}
@media (max-width: 760px) {
  .terminal-header {
    padding: 0 14px;
    height: 62px;
  }
  .brand strong {
    font-size: 17px;
  }
  .brand div > span {
    font-size: 7px;
    letter-spacing: 0.5px;
  }
  .brand-mark {
    width: 32px;
    line-height: 32px;
    font-size: 23px;
  }
  .brand {
    gap: 9px;
  }
  nav {
    display: none;
  }
  .ticker-strip {
    grid-template-columns: repeat(5, minmax(0, 1fr));
  }
  .ticker-strip button {
    padding: 7px 2px;
    font-size: 8px;
  }
  .ticker-strip button strong {
    font-size: 9px;
  }
  .ticker-strip button > span:last-child {
    font-size: 7px;
  }
  .market-heading {
    gap: 10px;
    margin: 22px 0 18px;
  }
  .market-heading .eyebrow {
    font-size: 7px;
    letter-spacing: 0.3px;
  }
  h1 {
    font-size: 29px;
    letter-spacing: -1px;
  }
  .market-heading p {
    font-size: 9px;
  }
  .heading-meta p {
    display: none;
  }
  .market-open {
    font-size: 9px;
  }
  .market-open b {
    display: none;
  }
  .tools {
    margin-top: 12px;
    flex-direction: column;
  }
  .tools button {
    font-size: 9px;
    padding: 3px 7px;
  }
  .overview {
    grid-template-columns: repeat(2, 1fr);
    margin-bottom: 16px;
  }
  .overview > div:nth-child(2) {
    border-right: 0;
  }
  .overview > div:nth-child(-n + 2) {
    border-bottom: 1px solid #ddd;
  }
  .overview strong {
    font-size: 21px;
  }
  .overview .unit {
    font-size: 8px;
  }
  .workspace {
    grid-template-columns: minmax(0, 1fr);
    gap: 16px;
  }
  .watchlist {
    grid-template-columns: minmax(0, 1fr);
  }
  .watchlist-item {
    display: none;
  }
  .watchlist-item.selected {
    display: flex;
    padding: 10px 14px;
  }
  .watchlist-name {
    display: flex;
  }
  .watchlist-name small {
    margin-left: 8px;
  }
  .watchlist-name strong {
    font-size: 12px;
  }
  .watchlist .panel-title {
    height: 35px;
  }
  .watchlist .panel-title h2 {
    font-size: 11px;
  }
  .quote-heading h2 {
    font-size: 21px;
  }
  .ohlc {
    gap: 9px;
    font-size: 8px;
    padding-left: 12px;
    padding-right: 12px;
  }
  .chart-legend {
    padding-left: 12px;
    font-size: 8px;
    gap: 9px;
  }
  .chart-footer {
    font-size: 7px;
  }
  .right-column {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
  }
  .promo-footer {
    flex-wrap: wrap;
    gap: 8px;
  }
  .promo-footer p {
    width: 100%;
    font-size: 8px;
  }
  .promo-footer > span {
    display: none;
  }
}
</style>
