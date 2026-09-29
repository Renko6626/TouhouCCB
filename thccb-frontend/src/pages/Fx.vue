<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import {
  computeMinOut,
  divideFxAmount,
  formatFxAmount,
  formatFxPrice,
  fxApi,
  FxOrderSubmitter,
  fxOrderSignature,
  FxStream,
  isConflictError,
  mapFxError,
  tradeSlippageBps,
} from '@/api/fx'
import { userApi } from '@/api/user'
import type {
  FxChartInterval,
  FxPairPublic,
  FxPriceTick,
  FxPublicFrame,
  FxPublicNews,
  FxQuote,
  FxSide,
  FxSnapshot,
  FxTradePublic,
  FxWalletPublic,
} from '@/types/fx'
import FxCandleChart from '@/components/chart/FxCandleChart.vue'

defineOptions({ name: 'FxPage' })

const msg = useMessage()

interface FxDisplaySummary {
  fx_mtm: number
  fx_cost_basis: number
  fx_unrealized_pnl: number
}

const loading = ref(true)
const error = ref<string | null>(null)
const pairs = ref<FxPairPublic[]>([])
const pairId = ref<number | null>(null)
const snapshot = ref<FxSnapshot | null>(null)
const trades = ref<FxTradePublic[]>([])
const wallet = ref<FxWalletPublic | null>(null)
const newsFeed = ref<FxPublicNews[]>([])
const streamConnected = ref(false)
/** 图表周期性刷新/成交后强制重载用；同时把 SSE 价格转发给图表组件 */
const chartReloadToken = ref(0)
const lastTick = ref<FxPriceTick | null>(null)
const priceDirection = ref<'up' | 'down' | 'neutral'>('neutral')

const intervals: FxChartInterval[] = ['1m', '15m', '1h']
const interval = ref<FxChartInterval>('1m')

const side = ref<FxSide>('buy')
const amount = ref('')
const slippageBps = ref<number>(50)
const quote = ref<FxQuote | null>(null)
const quoting = ref(false)
const submitting = ref(false)
const tradeError = ref<string | null>(null)
const orderSubmitter = new FxOrderSubmitter()

const summary = ref<FxDisplaySummary | null>(null)

const activePair = computed(() => pairs.value.find((p) => p.id === pairId.value) ?? null)
const tradable = computed(() => activePair.value?.status === 'trading')
const currencyName = computed(() => activePair.value?.currency_name ?? '外币')
/** 交易面板顶部按方向显示对应的有效买卖价 */
const sidePrice = computed(() =>
  side.value === 'buy' ? snapshot.value?.buy_price : snapshot.value?.sell_price,
)

const amountValid = computed(
  () => /^\d+(\.\d{0,6})?$/.test(amount.value.trim()) && Number(amount.value) > 0,
)
const effectiveSlippageBps = computed(() => {
  const v = Number(slippageBps.value)
  if (!Number.isFinite(v)) return 0
  return Math.max(0, Math.min(10000, Math.trunc(v)))
})
const minOut = computed(() =>
  quote.value ? computeMinOut(quote.value.output_amount, effectiveSlippageBps.value) : '',
)
const minOutDisplay = computed(() => (minOut.value ? formatFxAmount(minOut.value) : '—'))
// 后端 effective_price = output/input：buy 是「外币/金」，sell 是「金/外币」。
// 滑点统一折算成「金/外币」再与 snapshot.price（边际汇率，金/外币）比较。
const effectiveGoldPerForeign = computed(() => {
  const q = quote.value
  if (!q) return null
  const e = Number(q.effective_price)
  if (!Number.isFinite(e) || e <= 0) return null
  return side.value === 'buy' ? 1 / e : e
})
const quoteSlippage = computed(() =>
  effectiveGoldPerForeign.value !== null && snapshot.value
    ? tradeSlippageBps(snapshot.value.price, effectiveGoldPerForeign.value)
    : null,
)
const fxPnlPositive = computed(() => (summary.value?.fx_unrealized_pnl ?? 0) >= 0)
const walletAvgCost = computed(() => {
  const w = wallet.value
  if (!w) return null
  return divideFxAmount(w.cost_basis, w.foreign_amount, 12)
})

// ── 数据加载 ──
async function loadPairs() {
  pairs.value = await fxApi.listPairs()
  const preferred =
    pairs.value.find((p) => p.status === 'trading') ??
    pairs.value.find((p) => p.status !== 'draft') ??
    pairs.value[0]
  if (preferred) pairId.value = preferred.id
}

async function loadSnapshot() {
  const pid = pairId.value
  if (!pid) return
  snapshot.value = await fxApi.getSnapshot(pid)
}

async function loadTrades() {
  const pid = pairId.value
  if (!pid) return
  try {
    trades.value = await fxApi.getMyTrades(pid, 50)
  } catch {
    // 个人成交历史失败不阻塞行情与交易主流程
    trades.value = []
  }
}

async function loadWallet() {
  const pid = pairId.value
  if (!pid) return
  try {
    wallet.value = await fxApi.getWallet(pid)
  } catch {
    // 钱包读取失败时仅隐藏持仓明细，交易与行情仍可用
    wallet.value = null
  }
}

async function loadSummary() {
  try {
    const raw = (await userApi.getSummary()) as unknown as Partial<FxDisplaySummary>
    summary.value = {
      fx_mtm: Number(raw.fx_mtm) || 0,
      fx_cost_basis: Number(raw.fx_cost_basis) || 0,
      fx_unrealized_pnl: Number(raw.fx_unrealized_pnl) || 0,
    }
  } catch {
    // 净值面板失败不阻塞交易主流程
  }
}

async function refreshAll() {
  await Promise.allSettled([loadSnapshot(), loadTrades(), loadWallet(), loadSummary()])
}

// ── SSE ──
let stream: FxStream | null = null
function onFrame(frame: FxPublicFrame) {
  if (frame.price === undefined && frame.news === undefined) return
  const current = snapshot.value
  if (current) {
    const prev = Number(current.price)
    const next = frame.price !== undefined ? Number(frame.price) : null
    if (next !== null && Number.isFinite(next) && Number.isFinite(prev)) {
      if (next > prev) priceDirection.value = 'up'
      else if (next < prev) priceDirection.value = 'down'
    }
    snapshot.value = {
      ...current,
      price: frame.price ?? current.price,
      buy_price: frame.buy_price ?? current.buy_price,
      sell_price: frame.sell_price ?? current.sell_price,
      spread: frame.spread ?? current.spread,
      volume_24h: frame.volume ?? current.volume_24h,
    }
  }
  // 价格保持字符串语义转发给图表；图表内部才在适配层转 number
  if (frame.price !== undefined) lastTick.value = { price: frame.price, ts: Date.now() }
  if (frame.news) {
    const signature = `${frame.news.published_at ?? ''}|${frame.news.title ?? ''}`
    const exists = newsFeed.value.some(
      (n) => `${n.published_at ?? ''}|${n.title ?? ''}` === signature,
    )
    if (!exists) newsFeed.value = [frame.news, ...newsFeed.value].slice(0, 30)
  }
}

function connectStream() {
  const pid = pairId.value
  if (!pid) return
  if (!stream) {
    stream = new FxStream()
    stream.onOpen(() => {
      streamConnected.value = true
    })
    stream.onError(() => {
      streamConnected.value = false
    })
    stream.onFrame(onFrame)
  }
  stream.connect(pid)
}

async function selectPair(id: number) {
  pairId.value = id
  quote.value = null
  tradeError.value = null
  newsFeed.value = []
  wallet.value = null
  snapshot.value = null
  lastTick.value = null
  try {
    await Promise.all([loadSnapshot(), loadTrades(), loadWallet(), loadSummary()])
    connectStream()
  } catch (e) {
    error.value = mapFxError(e, 'FX 行情加载失败')
  }
}

async function load() {
  loading.value = true
  error.value = null
  try {
    await loadPairs()
    if (pairId.value === null) return
    await Promise.all([loadSnapshot(), loadTrades(), loadWallet(), loadSummary()])
    connectStream()
  } catch (e) {
    error.value = mapFxError(e, 'FX 行情加载失败')
  } finally {
    loading.value = false
  }
}

// ── 报价与成交 ──
let quoteTimer: ReturnType<typeof setTimeout> | null = null
let quoteGen = 0

function scheduleQuote() {
  quote.value = null
  if (quoteTimer) clearTimeout(quoteTimer)
  quoteTimer = setTimeout(() => {
    void fetchQuote()
  }, 350)
}

async function fetchQuote() {
  const pid = pairId.value
  const value = amount.value.trim()
  if (!pid || !tradable.value || !amountValid.value) {
    quote.value = null
    return
  }
  const gen = ++quoteGen
  quoting.value = true
  try {
    const q = await fxApi.getQuote(pid, { side: side.value, amount: value })
    if (gen === quoteGen) {
      quote.value = q
      tradeError.value = null
    }
  } catch (e) {
    if (gen === quoteGen) {
      quote.value = null
      tradeError.value = mapFxError(e, '报价失败')
    }
  } finally {
    if (gen === quoteGen) quoting.value = false
  }
}

async function submitTrade() {
  const pid = pairId.value
  if (!pid || submitting.value) return
  if (!tradable.value) {
    tradeError.value = '该货币对当前暂停或未开市，无法交易'
    return
  }
  if (!amountValid.value) {
    tradeError.value = '请输入有效的正数金额（最多 6 位小数）'
    return
  }
  // 同步占位：必须在第一个 await（获取报价）之前设置，双击只发一笔请求。
  submitting.value = true
  tradeError.value = null
  try {
    if (!quote.value) await fetchQuote()
    const q = quote.value
    if (!q) {
      tradeError.value = tradeError.value ?? '暂时拿不到报价，请稍后重试'
      return
    }
    const minOutValue = computeMinOut(q.output_amount, effectiveSlippageBps.value)
    const signature = fxOrderSignature({
      pairId: pid,
      side: side.value,
      amount: amount.value.trim(),
      minOut: minOutValue,
    })
    // 同一逻辑订单复用幂等键；并发在途时 submit 返回 null（不再发请求）。
    const trade = await orderSubmitter.submit(signature, (idempotencyKey) =>
      fxApi.trade(pid, {
        side: side.value,
        amount: amount.value.trim(),
        min_out: minOutValue,
        idempotency_key: idempotencyKey,
      }),
    )
    if (trade === null) return
    orderSubmitter.reset()
    msg.success(
      side.value === 'buy'
        ? `买入成功，预计到账 ${formatFxAmount(q.output_amount)} ${currencyName.value}`
        : `卖出成功，预计到账 ${formatFxAmount(q.output_amount)} 金圆券`,
    )
    amount.value = ''
    quote.value = null
    await refreshAll()
    chartReloadToken.value += 1
  } catch (e) {
    if (isConflictError(e)) {
      // 409：行情/幂等冲突，刷新 snapshot 后重新报价；参数一致时复用同一幂等键。
      await loadSnapshot().catch(() => {})
      await fetchQuote()
    }
    tradeError.value = mapFxError(e, '成交失败')
  } finally {
    submitting.value = false
  }
}

function setSide(next: FxSide) {
  if (side.value === next) return
  side.value = next
  scheduleQuote()
}

function setChartInterval(next: FxChartInterval) {
  if (interval.value === next) return
  interval.value = next
}

function onPairChange(event: Event) {
  const value = Number((event.target as HTMLSelectElement).value)
  if (Number.isFinite(value) && value > 0 && value !== pairId.value) void selectPair(value)
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isFinite(d.getTime()) ? d.toLocaleString() : '—'
}

const statusLabel = computed(() => {
  switch (activePair.value?.status) {
    case 'trading':
      return '交易中'
    case 'paused':
      return '暂停'
    case 'closed':
      return '已闭市'
    case 'draft':
      return '草稿'
    default:
      return '未知'
  }
})

watch([amount, side], scheduleQuote)

onMounted(load)

onUnmounted(() => {
  if (quoteTimer) clearTimeout(quoteTimer)
  stream?.disconnect()
  stream = null
})
</script>

<template>
  <div class="fx-page">
    <!-- ── 顶部行情条：货币对 / 状态 / 当前价 / 关键报价 ── -->
    <header class="fx-topbar">
      <div class="fx-topbar-id">
        <h1 class="fx-title">幻想外汇</h1>
        <div class="fx-pair-row">
          <select
            id="fx-pair"
            class="fx-pair-select"
            :value="pairId ?? ''"
            :disabled="pairs.length === 0"
            aria-label="选择货币对"
            @change="onPairChange"
          >
            <option v-if="pairs.length === 0" value="">暂无货币对</option>
            <option v-for="p in pairs" :key="p.id" :value="p.id">
              {{ p.currency_name }}（{{ p.currency_code }}）
            </option>
          </select>
          <span v-if="activePair" class="fx-status" :class="`fx-status-${activePair.status}`">
            {{ statusLabel }}
          </span>
          <span
            class="fx-stream-dot"
            :class="{ on: streamConnected }"
            :title="streamConnected ? '实时已连接' : '实时未连接'"
          ></span>
        </div>
      </div>

      <div class="fx-topbar-price">
        <span class="fx-topbar-label">边际汇率 · 金 / 1 {{ currencyName }}</span>
        <span class="fx-price" :class="priceDirection">{{ formatFxPrice(snapshot?.price) }}</span>
      </div>

      <div class="fx-topbar-stats">
        <div class="fx-stat">
          <span>买入价 ask</span>
          <b class="up">{{ formatFxPrice(snapshot?.buy_price) }}</b>
        </div>
        <div class="fx-stat">
          <span>卖出价 bid</span>
          <b class="down">{{ formatFxPrice(snapshot?.sell_price) }}</b>
        </div>
        <div class="fx-stat">
          <span>价差</span>
          <b>{{ formatFxPrice(snapshot?.spread) }}</b>
        </div>
        <div class="fx-stat">
          <span>24h 成交量</span>
          <b>{{ formatFxAmount(snapshot?.volume_24h) }}</b>
        </div>
      </div>
    </header>

    <div v-if="loading" class="fx-state">行情加载中…</div>
    <div v-else-if="error" class="fx-state fx-state-error">
      {{ error }}
      <button class="btn-secondary" @click="load">重试</button>
    </div>
    <div v-else-if="!activePair" class="fx-state">
      FX 尚未开市：管理员建立货币对、注资并开市后即可交易。
    </div>

    <template v-else>
      <div v-if="!tradable" class="fx-notice">
        当前货币对状态为「{{ statusLabel }}」，仅可查看行情，不能买卖。
      </div>

      <!-- ── 工作台：K 线主区 + 右侧交易面板（移动端堆叠） ── -->
      <div class="fx-workbench">
        <section class="fx-chart-panel">
          <div class="fx-panel-head">
            <div class="fx-panel-title">
              <h2>K 线</h2>
              <span class="fx-chart-sub">{{ currencyName }} · 金 / {{ currencyName }}</span>
            </div>
            <div class="fx-intervals">
              <button
                v-for="iv in intervals"
                :key="iv"
                class="fx-interval"
                :class="{ active: interval === iv }"
                @click="setChartInterval(iv)"
              >
                {{ iv }}
              </button>
            </div>
          </div>
          <div class="fx-chart-body">
            <FxCandleChart
              v-if="pairId"
              :pair-id="pairId"
              :interval="interval"
              :tick="lastTick"
              :reload-token="chartReloadToken"
              height="100%"
            />
          </div>
        </section>

        <aside class="fx-trade-panel">
          <div class="fx-trade-tabs">
            <button
              class="fx-trade-tab"
              :class="{ active: side === 'buy' }"
              @click="setSide('buy')"
            >
              买入 {{ currencyName }}
            </button>
            <button
              class="fx-trade-tab"
              :class="{ active: side === 'sell' }"
              @click="setSide('sell')"
            >
              卖出 {{ currencyName }}
            </button>
          </div>

          <div class="fx-trade-body">
            <div class="fx-trade-price">
              <span>{{ side === 'buy' ? '有效买入价（ask）' : '有效卖出价（bid）' }}</span>
              <strong :class="side === 'buy' ? 'up' : 'down'">
                {{ formatFxPrice(sidePrice) }}
              </strong>
            </div>

            <label class="fx-field">
              <span>{{ side === 'buy' ? '投入金圆券' : '投入外币' }}（最多 6 位小数）</span>
              <input
                v-model="amount"
                class="fx-input"
                inputmode="decimal"
                autocomplete="off"
                placeholder="0.000000"
                :disabled="!tradable || submitting"
              />
            </label>

            <label class="fx-field">
              <span>最大滑点（bps，100 = 1%）</span>
              <input
                v-model.number="slippageBps"
                class="fx-input"
                type="number"
                min="0"
                max="10000"
                step="1"
                :disabled="!tradable || submitting"
              />
            </label>

            <div class="fx-preview">
              <div class="fx-preview-row">
                <span>预计得到</span>
                <strong>{{ quote ? formatFxAmount(quote.output_amount) : '—' }}</strong>
              </div>
              <div class="fx-preview-row">
                <span>手续费</span>
                <strong>{{ quote ? formatFxAmount(quote.fee_amount) : '—' }}</strong>
              </div>
              <div class="fx-preview-row">
                <span>有效成交价（{{ side === 'buy' ? '外币/金' : '金/外币' }}）</span>
                <strong>{{ quote ? formatFxPrice(quote.effective_price) : '—' }}</strong>
              </div>
              <div class="fx-preview-row">
                <span>报价滑点 / 最大滑点</span>
                <strong>
                  {{ quoteSlippage === null ? '—' : quoteSlippage.toFixed(2) }} bps /
                  {{ effectiveSlippageBps }} bps
                </strong>
              </div>
              <div class="fx-preview-row fx-preview-row--minout">
                <span>min-out（服务端最低可接受产出）</span>
                <strong>{{ minOutDisplay }}</strong>
              </div>
            </div>

            <div class="fx-submit-state">
              <span v-if="tradeError" class="fx-error">{{ tradeError }}</span>
              <span v-else-if="submitting" class="fx-hint">提交中…服务端按幂等键防止重复扣款</span>
              <span v-else-if="quoting" class="fx-hint">报价更新中…</span>
              <span v-else class="fx-hint">
                成交按钮提交期间会禁用；价格冲突（409）会刷新行情并重新报价。
              </span>
            </div>

            <div class="fx-actions">
              <button
                class="fx-submit"
                :class="side === 'buy' ? 'fx-submit-buy' : 'fx-submit-sell'"
                :disabled="!tradable || submitting || !amountValid"
                @click="submitTrade"
              >
                {{ submitting ? '提交中…' : side === 'buy' ? '买入' : '卖出' }}
              </button>
              <button
                class="btn-secondary"
                :disabled="quoting || !amountValid"
                @click="fetchQuote"
              >
                重新报价
              </button>
            </div>
          </div>
        </aside>
      </div>

      <!-- ── 下方：持仓估值 / 新闻 / 成交记录 ── -->
      <div class="fx-lower">
        <section class="fx-block">
          <h2>外币持仓与估值</h2>
          <div class="fx-preview-row">
            <span>持仓数量（{{ currencyName }}）</span>
            <strong>{{ formatFxAmount(wallet?.foreign_amount ?? 0) }}</strong>
          </div>
          <div class="fx-preview-row">
            <span>持仓成本（金圆券）</span>
            <strong>{{ formatFxAmount(wallet?.cost_basis ?? 0) }}</strong>
          </div>
          <div class="fx-preview-row">
            <span>平均成本（金 / 外币）</span>
            <strong>{{ walletAvgCost === null ? '—' : formatFxPrice(walletAvgCost) }}</strong>
          </div>
          <div class="fx-preview-row">
            <span>持仓市值（MTM）</span>
            <strong>{{ formatFxAmount(summary?.fx_mtm ?? 0) }}</strong>
          </div>
          <div class="fx-preview-row">
            <span>浮动盈亏</span>
            <strong :class="fxPnlPositive ? 'up' : 'down'">
              {{ formatFxAmount(summary?.fx_unrealized_pnl ?? 0) }}
            </strong>
          </div>
          <p class="fx-collateral-note">
            FX 外币资产计入展示净值，<strong>不计入借款抵押价值</strong>，也不能直接用于预测市场、兑换商品或还款。
            有未还借款时不能买入外币，但可以卖出取回金圆券。
          </p>
        </section>

        <section class="fx-block">
          <h2>市场新闻</h2>
          <ul v-if="newsFeed.length" class="fx-news">
            <li v-for="(n, i) in newsFeed" :key="`${n.published_at}-${i}`">
              <div class="fx-news-title">{{ n.title || '未命名事件' }}</div>
              <div class="fx-news-body">{{ n.body }}</div>
              <div class="fx-news-meta">{{ n.kind || 'macro' }} · {{ formatTime(n.published_at) }}</div>
            </li>
          </ul>
          <p v-else class="fx-hint">当前没有已发布事件。新闻只包含公开标题与定性正文，不含隐藏冲击数值。</p>
        </section>

        <section class="fx-block fx-trades-block">
          <div class="fx-panel-head">
            <h2>我的成交记录</h2>
            <button class="btn-secondary" @click="loadTrades">刷新</button>
          </div>
          <div class="table-wrap">
            <table class="fx-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>方向</th>
                  <th>投入</th>
                  <th>产出</th>
                  <th>手续费</th>
                  <th>成交后价格</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="t in trades" :key="t.id">
                  <td>{{ formatTime(t.created_at) }}</td>
                  <td :class="t.side === 'buy' ? 'up' : 'down'">
                    {{ t.side === 'buy' ? '买外币' : '卖外币' }}
                  </td>
                  <td>{{ formatFxAmount(t.input_amount) }}</td>
                  <td>{{ formatFxAmount(t.output_amount) }}</td>
                  <td>{{ formatFxAmount(t.fee_amount) }}</td>
                  <td>{{ formatFxPrice(t.post_price) }}</td>
                </tr>
                <tr v-if="trades.length === 0">
                  <td colspan="6" class="fx-empty-cell">暂无成交</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="fx-hint">
            只显示当前登录用户的个人成交；公开行情（价格/K 线/新闻）见上方，不包含任何其他用户身份或隐藏事件参数。
          </p>
        </section>
      </div>
    </template>
  </div>
</template>

<style scoped>
.fx-page {
  padding: 4px;
  max-width: 1360px;
}
.fx-title {
  margin: 0;
  font-size: 18px;
  font-weight: 800;
  letter-spacing: 0.02em;
}
/* ── 顶部行情条 ── */
.fx-topbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px 28px;
  border: 2px solid #000;
  background: #fff;
  padding: 10px 14px;
  margin-bottom: 12px;
}
.fx-topbar-id {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.fx-pair-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.fx-pair-select {
  border: 2px solid #000;
  background: #fff;
  padding: 5px 8px;
  font-family: inherit;
  font-size: 13px;
  font-weight: 700;
}
.fx-status {
  border: 1.5px solid #000;
  padding: 1px 8px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.fx-status-trading {
  background: #000;
  color: #fff;
}
.fx-status-paused,
.fx-status-closed,
.fx-status-draft {
  background: #fff;
  color: #555;
}
.fx-stream-dot {
  width: 10px;
  height: 10px;
  border: 1.5px solid #000;
  background: #fff;
  display: inline-block;
}
.fx-stream-dot.on {
  background: #16a34a;
}
.fx-topbar-price {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 220px;
}
.fx-topbar-label {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #777;
}
.fx-price {
  font-size: 34px;
  font-weight: 800;
  line-height: 1.05;
  font-variant-numeric: tabular-nums;
}
.fx-price.up { color: var(--color-up, #16a34a); }
.fx-price.down { color: var(--color-down, #dc2626); }
.fx-topbar-stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(110px, 1fr));
  gap: 6px 22px;
  flex: 1;
  min-width: 260px;
}
.fx-stat {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.fx-stat span {
  font-size: 11px;
  color: #777;
}
.fx-stat b {
  font-size: 15px;
  font-variant-numeric: tabular-nums;
}
.fx-stat b.up { color: var(--color-up, #16a34a); }
.fx-stat b.down { color: var(--color-down, #dc2626); }

/* ── 状态 ── */
.fx-state {
  border: 2px solid #000;
  padding: 24px;
  background: #fff;
  font-weight: 600;
  display: flex;
  gap: 12px;
  align-items: center;
}
.fx-state-error {
  color: var(--color-down, #dc2626);
}
.fx-notice {
  border: 2px solid #b45309;
  background: #fffbeb;
  color: #92400e;
  padding: 8px 12px;
  margin-bottom: 12px;
  font-size: 13px;
  font-weight: 600;
}

/* ── 工作台 ── */
.fx-workbench {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 340px;
  border: 2px solid #000;
  background: #fff;
}
.fx-chart-panel {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.fx-trade-panel {
  display: flex;
  flex-direction: column;
  border-left: 2px solid #000;
}
.fx-panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 14px;
  border-bottom: 1px solid #e0e0e0;
}
.fx-panel-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.fx-panel-title h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 800;
}
.fx-chart-sub {
  font-size: 12px;
  color: #888;
  font-variant-numeric: tabular-nums;
}
.fx-intervals {
  display: flex;
  gap: 6px;
}
.fx-interval {
  border: 1.5px solid #000;
  background: #fff;
  padding: 2px 12px;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}
.fx-interval.active {
  background: #000;
  color: #fff;
}
.fx-chart-body {
  height: 540px;
  min-height: 0;
}

/* ── 交易面板 ── */
.fx-trade-tabs {
  display: flex;
  border-bottom: 2px solid #000;
}
.fx-trade-tab {
  flex: 1;
  padding: 10px 6px;
  background: #fff;
  border: none;
  cursor: pointer;
  font-weight: 800;
  font-size: 13px;
  color: #555;
}
.fx-trade-tab + .fx-trade-tab {
  border-left: 1px solid #000;
}
.fx-trade-tab.active {
  background: #000;
  color: #fff;
}
.fx-trade-body {
  padding: 12px 14px 14px;
  display: flex;
  flex-direction: column;
}
.fx-trade-price {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
  border-bottom: 1px solid #e0e0e0;
  padding-bottom: 8px;
  margin-bottom: 10px;
}
.fx-trade-price span {
  font-size: 12px;
  color: #666;
}
.fx-trade-price strong {
  font-size: 20px;
  font-variant-numeric: tabular-nums;
}
.fx-trade-price strong.up { color: var(--color-up, #16a34a); }
.fx-trade-price strong.down { color: var(--color-down, #dc2626); }
.fx-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 10px;
  font-size: 12px;
  color: #444;
}
.fx-input {
  border: 2px solid #000;
  padding: 8px 10px;
  font-family: ui-monospace, monospace;
  font-size: 14px;
  background: #fff;
}
.fx-input:disabled {
  background: #f5f5f5;
  color: #888;
}
.fx-preview {
  border: 1.5px solid #000;
  padding: 8px 10px;
  margin: 2px 0 10px;
  background: #fafafa;
}
.fx-preview-row {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: 13px;
  padding: 3px 0;
}
.fx-preview-row span {
  color: #666;
}
.fx-preview-row strong {
  font-variant-numeric: tabular-nums;
  text-align: right;
}
.fx-preview-row strong.up { color: var(--color-up, #16a34a); }
.fx-preview-row strong.down { color: var(--color-down, #dc2626); }
.fx-preview-row--minout strong {
  font-family: ui-monospace, monospace;
  font-size: 12px;
}
.fx-submit-state {
  min-height: 34px;
  display: flex;
  align-items: flex-start;
}
.fx-error {
  color: var(--color-down, #dc2626);
  font-size: 12px;
  font-weight: 600;
  margin: 0;
}
.fx-hint {
  font-size: 12px;
  color: #777;
  line-height: 1.5;
  margin: 0;
}
.fx-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
}
.fx-submit {
  flex: 1;
  min-width: 120px;
  border: 2px solid #000;
  background: #000;
  color: #fff;
  padding: 10px 16px;
  font-size: 14px;
  font-weight: 800;
  cursor: pointer;
}
.fx-submit:disabled {
  background: #999;
  border-color: #999;
  cursor: not-allowed;
}
.fx-submit-sell {
  background: #fff;
  color: #000;
}
.fx-submit-sell:disabled {
  background: #f0f0f0;
  color: #999;
  border-color: #999;
}
.fx-submit:not(:disabled):hover {
  transform: translate(-1px, -1px);
  box-shadow: 3px 3px 0 #000;
}

/* ── 下方区块 ── */
.fx-lower {
  margin-top: 12px;
  border: 2px solid #000;
  background: #fff;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
}
.fx-block {
  padding: 14px;
  min-width: 0;
}
.fx-block:nth-child(2) {
  border-left: 2px solid #000;
}
.fx-block h2 {
  margin: 0 0 10px;
  font-size: 15px;
  font-weight: 800;
}
.fx-trades-block {
  grid-column: 1 / -1;
  border-top: 2px solid #000;
}
.fx-trades-block .fx-panel-head {
  padding: 0 0 8px;
  border-bottom: none;
}
.fx-collateral-note {
  margin: 10px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: #444;
  border-left: 3px solid #000;
  padding-left: 8px;
}
.fx-news {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 300px;
  overflow-y: auto;
}
.fx-news li {
  border-bottom: 1px solid #e0e0e0;
  padding: 8px 0;
}
.fx-news-title {
  font-weight: 700;
  font-size: 13px;
}
.fx-news-body {
  font-size: 12px;
  color: #444;
  margin-top: 2px;
  line-height: 1.5;
}
.fx-news-meta {
  font-size: 11px;
  color: #999;
  margin-top: 4px;
}
.table-wrap {
  margin-top: 8px;
  overflow-x: auto;
  border: 2px solid #000;
}
.fx-table {
  width: 100%;
  border-collapse: collapse;
  background: #fff;
}
.fx-table th,
.fx-table td {
  border: 1px solid #ddd;
  padding: 6px 10px;
  text-align: left;
  white-space: nowrap;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.fx-table th {
  background: #000;
  color: #fff;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-size: 11px;
}
.fx-table td.up { color: var(--color-up, #16a34a); }
.fx-table td.down { color: var(--color-down, #dc2626); }
.fx-empty-cell {
  text-align: center;
  color: #888;
}

@media (max-width: 1024px) {
  .fx-workbench {
    grid-template-columns: 1fr;
  }
  .fx-trade-panel {
    border-left: none;
    border-top: 2px solid #000;
  }
  .fx-chart-body {
    height: 400px;
  }
  .fx-lower {
    grid-template-columns: 1fr;
  }
  .fx-block:nth-child(2) {
    border-left: none;
    border-top: 2px solid #000;
  }
  .fx-topbar-stats {
    grid-template-columns: repeat(2, minmax(110px, 1fr));
  }
}
@media (max-width: 560px) {
  .fx-price {
    font-size: 26px;
  }
  .fx-chart-body {
    height: 320px;
  }
  .fx-submit {
    flex-basis: 100%;
  }
}
</style>
