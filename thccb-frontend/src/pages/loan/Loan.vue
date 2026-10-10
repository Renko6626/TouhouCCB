<script setup lang="ts">
import { onMounted, ref, computed, watch } from 'vue'
import { NInputNumber, NButton, NSpin, NAlert, useMessage } from 'naive-ui'
import { useLoanStore } from '@/stores/loan'
import { fetchLiquidationPolicy, type LiquidationPolicy } from '@/api/loan'
import { loanOperationError } from '@/utils/errors'
import { compareFxAmounts, formatFxAmount, subtractFxAmounts } from '@/api/fx'
import type { AccountShortPosition } from '@/types/user'
import ShortPositionPnl from '@/components/user/ShortPositionPnl.vue'
import CreditRiskStatus from '@/components/user/CreditRiskStatus.vue'

defineOptions({ name: 'LoanPage' })

const store = useLoanStore()
const msg = useMessage()

const borrowAmount = ref<number | null>(null)
const repayAmount = ref<number | null>(null)
const submitting = ref(false)
const busy = computed(() => store.loading || submitting.value)

const policy = ref<LiquidationPolicy | null>(null)
const policyError = ref(false)

onMounted(async () => {
  store.refresh()
  try {
    policy.value = await fetchLiquidationPolicy()
  } catch {
    policyError.value = true
  }
})

const borrowPresets = [100, 500, 1000]

function formatPercent(value: string | number | null | undefined) {
  if (value == null || !Number.isFinite(Number(value))) return '—'
  return `${(Number(value) * 100).toFixed(2)}%`
}

const dailyRatePct = computed(() => {
  const r = store.quota?.daily_rate
  if (!r) return '—'
  return (Number(r) * 100).toFixed(2) + '%'
})

const borrowRestriction = computed(() => {
  const labels: Record<string, string> = {
    loan_disabled: '借款功能已关闭。',
    frozen_by_operator: '运营已暂停新增风险，仍可还款或回补。',
    credit_frozen: '账户信用已冻结，仍可还款或回补。',
    valuation_unavailable: '账户估值暂不可用，请刷新后重试。',
    insufficient_initial_margin: '账户低于借款保证金门槛，请先还款或减少风险。',
    no_borrow_headroom: '当前没有可用借款额度。',
  }
  const reason = store.quota?.borrow_blocked_reason
  return reason ? labels[reason] ?? '暂时无法新增借款，请刷新账户信息。' : null
})

const shortPositions = computed(() => store.quota?.short_positions ?? [])
const marginRatio = computed(() => store.quota?.equity_to_risk_basis ?? null)
function shortRestriction(reason: string | null | undefined) {
  const labels: Record<string, string> = {
    fx_disabled: '外汇交易已暂停，参考回补成本仍有效。',
    pair_paused: '该币种交易已暂停。',
    pair_closed: '该币种交易已关闭。',
    insufficient_pool_foreign: '池内外币不足，暂无法报价全仓回补。',
    invalid_short_debt: '外币债务数据异常，请联系管理员。',
    short_quote_failed: '暂无法计算全仓回补报价，请稍后重试。',
  }
  return reason ? labels[reason] ?? '暂无法执行全仓回补，请查看具体报价。' : '暂无法执行全仓回补，请查看具体报价。'
}

function pendingInterest(position: AccountShortPosition) {
  if (position.pending_short_debt == null) return null
  const interest = subtractFxAmounts(position.pending_short_debt, position.principal_foreign)
  return interest == null ? null : subtractFxAmounts(interest, position.interest_foreign)
}

const debtNumber = computed(() => Number(store.quota?.debt ?? '0'))
const cashAmount = computed(() => store.quota?.available_cash ?? null)
const cashNumber = computed(() => Number(cashAmount.value ?? '0'))
const maxBorrowNumber = computed(() => Number(store.quota?.max_borrow ?? '0'))
// 还款上限：min(真实负债, 真实现金) — 与服务端封顶逻辑对齐
const maxRepayNumber = computed(() => Math.min(debtNumber.value, cashNumber.value))

// 用户输入超出实际能扣减部分时的预览
const repayOverflow = computed(() => {
  const v = Number(repayAmount.value ?? 0)
  const cap = maxRepayNumber.value
  if (v > 0 && v > cap) return v - cap
  return 0
})

watch(() => store.error, (error) => {
  if (error?.startsWith('操作已完成，账户信息刷新失败：')) msg.warning(error)
})

async function submitBorrow() {
  if (busy.value || !store.quota?.enabled || !borrowAmount.value || borrowAmount.value <= 0
    || borrowAmount.value > maxBorrowNumber.value) return
  submitting.value = true
  try {
    const amount = String(borrowAmount.value)
    const result = await store.borrow(amount)
    msg.success(`借入 金 ${formatFxAmount(result.effective ?? amount, 2)}`)
    borrowAmount.value = null
  } catch (e: unknown) {
    msg.error(loanOperationError(e, '借款失败'))
  } finally {
    submitting.value = false
  }
}

async function submitRepay() {
  if (busy.value || !repayAmount.value || repayAmount.value <= 0 || repayAmount.value > maxRepayNumber.value) return
  submitting.value = true
  try {
    const r = await store.repay(String(repayAmount.value))
    const effective = r.effective ?? String(repayAmount.value)
    msg.success(`实际还款 金 ${formatFxAmount(effective, 2)}`)
    repayAmount.value = null
  } catch (e: unknown) {
    msg.error(loanOperationError(e, '还款失败'))
  } finally {
    submitting.value = false
  }
}

async function repayAll() {
  if (busy.value || !store.quota || debtNumber.value <= 0 || cashNumber.value <= 0) return
  submitting.value = true
  try {
    const r = await store.repayAll()
    if (/^0(?:\.0+)?$/.test(r.debt)) {
      msg.success(`已全部还清（实际还款 金 ${r.effective ?? '0'}）`)
    } else {
      msg.success(`已用可用现金还款 金 ${r.effective ?? '0'}，剩余借款 金 ${r.debt}（含利息）`)
    }
    repayAmount.value = null
  } catch (e: unknown) {
    msg.error(loanOperationError(e, '还款失败'))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="loan-page">
    <header class="loan-header">
      <div>
        <h1>借款与还款</h1>
        <p>借入金圆券用于交易，用可用现金还款。</p>
      </div>
      <NButton size="small" :disabled="busy" @click="store.refresh">刷新</NButton>
    </header>

    <NSpin :show="store.loading">
      <NAlert v-if="store.error" class="page-alert" type="error" :title="store.error" />
      <NAlert v-else-if="store.quota && !store.quota.enabled" class="page-alert" type="warning" title="借款功能维护中，已有借款仍可还款" />
      <NAlert v-if="borrowRestriction" class="page-alert" type="warning" title="借款限制">
        {{ borrowRestriction }}
      </NAlert>
      <NAlert v-if="store.quota?.risk_status === 'blocked'" class="page-alert" type="warning" title="暂时无法新增借款或风险">
        账户估值暂不可用，请刷新后重试。
        <small v-if="store.quota.blocked_reason">诊断原因：{{ store.quota.blocked_reason }}</small>
      </NAlert>
      <NAlert v-else-if="store.quota?.risk_status === 'danger'" class="page-alert" type="error" title="有强平风险，请优先还款或回补">
        {{ policy?.enabled === false ? '强平检查目前暂停；恢复后会按最新账户状态检查。' : '系统可能自动卖出持仓或回补外币来还债。' }}
      </NAlert>
      <NAlert v-else-if="store.quota?.risk_status === 'warning'" class="page-alert" type="warning" title="账户低于开仓门槛，请关注风险">
        新增风险可能受限，可尝试还款或回补减仓。
      </NAlert>

      <div class="loan-workbench">
        <section id="gold-loan" class="panel gold-loan-panel" aria-labelledby="gold-loan-heading">
          <h2 id="gold-loan-heading">普通借款 <span>金圆券</span></h2>
          <dl class="loan-stats">
            <div>
              <dt>还能借</dt>
              <dd>{{ formatFxAmount(store.quota?.max_borrow, 2) }}</dd>
            </div>
            <div>
              <dt>待还（含利息）</dt>
              <dd :class="{ red: debtNumber > 0 }">{{ formatFxAmount(store.quota?.debt, 2) }}</dd>
            </div>
            <div>
              <dt>可用现金</dt>
              <dd>{{ formatFxAmount(cashAmount, 2) }}</dd>
            </div>
          </dl>

          <div class="loan-actions">
            <section class="loan-action" aria-labelledby="borrow-heading">
              <div class="action-heading">
                <h3 id="borrow-heading">借款</h3>
                <span>日利率 {{ dailyRatePct }}</span>
              </div>
              <label for="borrow-amount" class="field-label">借多少金圆券</label>
              <div class="amount-row">
                <NInputNumber
                  v-model:value="borrowAmount"
                  size="small"
                  placeholder="输入金额"
                  :min="0.01"
                  :max="maxBorrowNumber"
                  :precision="2"
                  :input-props="{ id: 'borrow-amount', inputmode: 'decimal' }"
                  :disabled="busy || !store.quota?.enabled || maxBorrowNumber <= 0"
                  class="amount-input"
                />
                <NButton
                  size="small"
                  type="primary"
                  :loading="submitting"
                  :disabled="busy || !store.quota?.enabled || !borrowAmount || borrowAmount <= 0 || borrowAmount > maxBorrowNumber"
                  @click="submitBorrow"
                >确认借款</NButton>
              </div>
              <div class="quick-amounts" aria-label="快捷填写借款金额">
                <NButton
                  v-for="amount in borrowPresets"
                  :key="amount"
                  size="tiny"
                  :disabled="busy || !store.quota?.enabled || amount > maxBorrowNumber"
                  @click="borrowAmount = amount"
                >金 {{ amount }}</NButton>
              </div>
              <p class="action-note">借款到账后可自行交易，未还借款会产生利息。</p>
              <p v-if="store.quota?.enabled && maxBorrowNumber <= 0" class="action-warning" role="status">当前没有可借额度，请先还款或等待账户风险恢复。</p>
              <p v-if="borrowAmount && borrowAmount > maxBorrowNumber" class="action-warning" role="status">超过当前可借额度，请减少金额。</p>
            </section>

            <section class="loan-action" aria-labelledby="repay-heading">
              <div class="action-heading">
                <h3 id="repay-heading">还款</h3>
                <span>最多可还 金 {{ store.quota && cashAmount != null ? formatFxAmount(maxRepayNumber, 2) : '—' }}</span>
              </div>
              <label for="repay-amount" class="field-label">还多少金圆券</label>
              <div class="amount-row">
                <NInputNumber
                  v-model:value="repayAmount"
                  size="small"
                  placeholder="输入金额"
                  :min="0.01"
                  :max="maxRepayNumber"
                  :precision="2"
                  :input-props="{ id: 'repay-amount', inputmode: 'decimal' }"
                  :disabled="busy || debtNumber <= 0 || cashNumber <= 0"
                  class="amount-input"
                />
                <NButton
                  size="small"
                  :loading="submitting"
                  :disabled="busy || !repayAmount || repayAmount <= 0 || debtNumber <= 0 || cashNumber <= 0 || repayAmount > maxRepayNumber"
                  @click="submitRepay"
                >确认还款</NButton>
              </div>
              <NButton
                size="small"
                class="repay-all"
                :loading="submitting"
                :disabled="busy || !store.quota || debtNumber <= 0 || cashNumber <= 0"
                @click="repayAll"
              >{{ cashNumber >= debtNumber ? '一键还清' : '用全部可用现金还款' }}</NButton>
              <p v-if="store.quota && debtNumber <= 0" class="action-note">暂无金圆券借款，无需还款。</p>
              <p v-else-if="store.quota && cashAmount == null" class="action-warning">可用现金暂不可用，请刷新后重试。</p>
              <p v-else-if="store.quota && cashNumber <= 0" class="action-warning">没有可用现金，可先卖出持仓再还款。</p>
              <p v-else class="action-note">按提交时的含息欠款还款，现金不足时仍会保留欠款。</p>
              <p v-if="repayOverflow > 0" class="action-warning" role="status">超过当前可还金额，请减少金额或使用一键还款。</p>
            </section>
          </div>
          <p class="loan-footnote">金额单位：金圆券。额度随行情变化，实际以提交时为准。</p>
        </section>

        <aside class="panel risk-panel" aria-label="账户风险">
          <CreditRiskStatus
            title="保证金率（越高越安全）"
            :ratio="marginRatio"
            :initial="store.quota?.r_initial ?? policy?.r_initial"
            :maintenance="store.quota?.r_maintenance ?? policy?.r_maintenance"
            :authoritative-status="store.loading || store.error ? 'unknown' : store.quota?.risk_status"
            :no-risk="compareFxAmounts(store.quota?.risk_basis, '0') === 0"
          />
          <p class="risk-note">低于强平线时，系统可能自动减仓还债。</p>
          <p v-if="policy?.enabled === false" class="action-warning">强平检查目前暂停。</p>
          <a class="details-link" href="#loan-rules">查看额度与强平规则</a>
        </aside>
      </div>

      <section id="short-debt" class="panel short-debt-panel" aria-labelledby="short-debt-heading">
        <div class="section-heading">
          <div>
            <h2 id="short-debt-heading">外币空头 <span>{{ store.quota ? shortPositions.length : '—' }} 笔</span></h2>
            <p class="section-intro">借的是外币，需买回同种外币归还。</p>
          </div>
          <router-link to="/fx" class="details-link">前往外汇</router-link>
        </div>
        <p v-if="!store.quota" class="short-empty">{{ store.error ? '读取失败，请刷新后查看。' : '正在读取外币负债…' }}</p>
        <div v-else-if="shortPositions.length" class="short-debt-list">
          <article v-for="position in shortPositions" :key="position.pair_id" class="short-debt-card">
            <div class="section-heading">
              <h3>{{ position.currency_code }}</h3>
              <span class="position-status" :class="{ blocked: !position.executable }">{{ position.executable ? '可获取回补报价' : '回补受限' }}</span>
            </div>
            <div class="short-debt-amount">
              <span>待归还（含利息）</span>
              <strong>{{ formatFxAmount(position.pending_short_debt, 6) }} <small>{{ position.currency_code }}</small></strong>
            </div>
            <ShortPositionPnl :proceeds-basis-gold="position.proceeds_basis_gold" :reference-cover-cost="position.reference_cover_cost" />
            <dl class="short-details">
              <div><dt>锁定用于回补</dt><dd>金 {{ formatFxAmount(position.restricted_gold, 2) }}</dd></div>
              <div><dt>预计全部买回需花费</dt><dd>{{ position.reference_cover_cost == null ? '估值待恢复' : `金 ${formatFxAmount(position.reference_cover_cost, 2)}` }}</dd></div>
            </dl>
            <p v-if="!position.executable || position.blocked_reason" class="position-warning">{{ shortRestriction(position.blocked_reason) }}</p>
            <router-link :to="{ path: '/fx', query: { pair: position.pair_id, action: 'cover' } }" class="cover-link">买回归还 {{ position.currency_code }}</router-link>
            <details class="position-details">
              <summary>本金、利息与费用</summary>
              <dl class="short-details">
                <div><dt>借入本金</dt><dd>{{ formatFxAmount(position.principal_foreign, 6) }} {{ position.currency_code }}</dd></div>
                <div><dt>已结利息 / 待计利息</dt><dd>{{ formatFxAmount(position.interest_foreign, 6) }} / {{ formatFxAmount(pendingInterest(position), 6) }} {{ position.currency_code }}</dd></div>
                <div><dt>参考回补手续费（已含）</dt><dd>金 {{ formatFxAmount(position.reference_cover_fee) }}</dd></div>
              </dl>
              <p>回补成本含利息、手续费与滑点，以成交报价为准。</p>
            </details>
          </article>
        </div>
        <p v-else class="short-empty">暂无待归还的外币。</p>
        <p v-if="shortPositions.length" class="short-note">空头卖出所得锁定用于回补，不能用于消费或偿还金圆券借款。全部资产共享保证金。</p>
      </section>

      <section id="loan-rules" class="panel rules-panel" aria-labelledby="loan-rules-heading">
        <h2 id="loan-rules-heading">额度与强平规则</h2>
        <NAlert v-if="!policy" :type="policyError ? 'warning' : 'info'" :title="policyError ? '规则加载失败，请刷新页面重试' : '正在读取借款规则'" />
        <template v-else>
          <details class="rule-details">
            <summary>能借多少？利息怎么算？</summary>
            <div class="rule-content">
              <p>日利率 {{ dailyRatePct }}，未还借款会持续计息。<span v-if="store.quota?.last_accrued_at">上次结息：{{ new Date(store.quota.last_accrued_at).toLocaleString() }}。</span></p>
                <p>借入的金圆券可用于预测市场和外汇交易。额度由现金及可变现的持仓共同支持，已有借款和外币回补义务都会占用额度。</p>
                <p v-if="policy.credit_leverage != null">名义杠杆上限 {{ policy.credit_leverage }} 倍；没有外币空头时，借款不得超过清算净值的 {{ Number((policy.credit_leverage - 1).toFixed(6)) }} 倍。借款不会自动买入持仓，也不代表已使用这个倍数。</p>
                <p>行情、手续费和滑点都会影响额度。借入和买入时会重新检查，借满后也可能因交易成本而无法继续买入。</p>
              <p>一键还款按提交时的最新含息欠款和可用现金扣款，不会多扣。外币需先卖成金圆券才能还金圆券借款。</p>
            </div>
          </details>

          <details class="rule-details">
            <summary>什么情况下会被自动减仓？</summary>
            <div class="rule-content">
                <dl class="rule-metrics">
                  <div><dt>新增风险 / 恢复门槛</dt><dd>{{ formatPercent(policy.r_initial) }}</dd></div>
                  <div><dt>强平触发线</dt><dd class="red">低于 {{ formatPercent(policy.r_maintenance) }}</dd></div>
                  <div><dt>检查间隔</dt><dd>{{ policy.sweep_interval_sec }} 秒</dd></div>
                  <div><dt>每次通常处理</dt><dd>{{ formatPercent(policy.partial_pct) }}</dd></div>
                </dl>
                <p>任一持仓亏损都会影响共用额度。保证金率低于强平线时，系统会卖出资产或回补外币来还债，其他预测市场或外汇持仓也可能受影响。</p>
                <p>每人每次扫描最多处理一组资产或外币义务，有待回补义务时保留现金用于回补。清算净值不大于 0 时扩大减仓，回补仍受实际现金与市场容量限制。</p>
                <p>后续检查继续处理，恢复到初始率或还清即停止；卖光仍欠债会冻结新增信用，不会免债。</p>
                <p>强平不另收罚金。预测市场卖出费率 {{ formatPercent(policy.sell_fee_rate ?? 0) }}。<span v-for="pair in policy.fx_sell_fee_rates" :key="pair.pair_id">{{ pair.currency_code }} 卖出费率 {{ formatPercent(pair.sell_fee_rate) }}。</span></p>
              <p>强平检查{{ policy.enabled ? '已开启' : '已暂停' }}。交易或行情变化不会额外触发强平。主动还款、减少风险持仓有助于降低强平风险。</p>
            </div>
          </details>

          <details v-if="store.quota" class="rule-details">
            <summary>查看现金与风险计算明细</summary>
            <div class="rule-content">
              <dl class="rule-metrics">
                <div><dt>总现金</dt><dd>金 {{ formatFxAmount(store.quota.cash) }}</dd></div>
                <div><dt>未锁定现金</dt><dd>金 {{ formatFxAmount(store.quota.available_cash) }}</dd></div>
                <div><dt>锁定空头所得</dt><dd>金 {{ formatFxAmount(store.quota.restricted_cash) }}</dd></div>
                <div><dt>全仓回补成本 K</dt><dd>金 {{ formatFxAmount(store.quota.short_cover_cost) }}</dd></div>
                <div><dt>风险基数 B</dt><dd>金 {{ formatFxAmount(store.quota.risk_basis) }}</dd></div>
                <div><dt>账面净值</dt><dd>金 {{ formatFxAmount(store.quota.display_equity) }}</dd></div>
                <div><dt>清算净值 E</dt><dd>金 {{ formatFxAmount(store.quota.liquidation_equity) }}</dd></div>
                <div><dt>保证金率 E/B</dt><dd>{{ formatPercent(store.quota.equity_to_risk_basis) }}</dd></div>
              </dl>
              <p>清算净值 E = 总现金 + 预测市场与外汇多头实际可变现金额 − 含息金债 − 外币全仓回补成本 K。</p>
              <p>锁定空头所得已包含在总现金中，专用于本仓回补。不可卖资产的清算价值为 0；无法完整报价的外币负债不能按 0 处理，会阻塞风险检查。</p>
            </div>
          </details>
        </template>
      </section>
    </NSpin>
  </div>
</template>

<style scoped>
.loan-page { padding: 12px; max-width: 1100px; margin: 0 auto; }
.loan-header, .section-heading, .action-heading { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.loan-header { margin-bottom: 12px; }
.loan-header h1 { font-size: 22px; font-weight: 800; line-height: 1.3; }
.loan-header p { margin-top: 4px; color: #666; font-size: 12px; }
.panel { border: 2px solid #000; padding: var(--trade-panel-padding); background: #fff; min-width: 0; }
.page-alert { margin-bottom: 10px; }
h2, h3 { font-size: 14px; font-weight: 700; }
h2 > span { margin-left: 6px; font-size: 12px; color: #666; }
.red { color: var(--color-down); }
.loan-workbench { display: grid; grid-template-columns: minmax(0, 1fr) 270px; gap: 12px; align-items: start; margin-bottom: 12px; }
.loan-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); margin: 10px 0 12px; padding: 10px 0; border-top: 1px solid #ddd; border-bottom: 1px solid #ddd; }
.loan-stats > div { padding: 0 10px; min-width: 0; border-left: 1px solid #ddd; }
.loan-stats > div:first-child { padding-left: 0; border-left: 0; }
.loan-stats dt { color: #666; font-size: 12px; }
.loan-stats dd { font-size: 22px; font-weight: 800; font-variant-numeric: tabular-nums; line-height: 1.4; overflow-wrap: anywhere; }
.loan-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.loan-action { min-width: 0; }
.action-heading { margin-bottom: 8px; flex-wrap: wrap; }
.action-heading > span { font-size: 11px; color: #666; }
.field-label { display: block; margin-bottom: 4px; font-size: 12px; color: #555; }
.amount-row { display: flex; align-items: center; gap: 6px; }
.amount-input { flex: 1; min-width: 0; }
.quick-amounts { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 8px; }
.repay-all { width: 100%; margin-top: 8px; }
.action-note, .action-warning, .loan-footnote, .risk-note, .section-intro, .short-note, .short-empty { font-size: 12px; line-height: 1.5; color: #666; }
.action-note, .action-warning { margin-top: 6px; }
.action-warning { color: var(--color-down); }
.loan-footnote { margin-top: 12px; padding-top: 8px; border-top: 1px solid #ddd; }
.risk-panel :deep(.credit-risk) { gap: 6px; }
.risk-panel :deep(.credit-risk-ratio) { font-size: 22px; font-weight: 800; line-height: 1.3; }
.risk-panel :deep(.credit-risk-thresholds) { font-size: 11px; }
.risk-note { margin: 8px 0; }
.details-link { color: #000; font-size: 12px; font-weight: 700; text-underline-offset: 3px; }
.short-debt-panel { margin-bottom: 12px; }
.section-heading { flex-wrap: wrap; }
.section-intro { margin-top: 4px; }
.short-debt-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
.short-debt-card { padding: 10px; border: 1px solid #000; background: #fafafa; min-width: 0; }
.short-debt-card h3 { font-size: 15px; }
.position-status { font-size: 11px; color: #666; }
.position-status.blocked, .position-warning { color: var(--color-down); }
.short-debt-amount { margin: 10px 0 6px; }
.short-debt-amount > span { font-size: 11px; color: #666; }
.short-debt-amount strong { display: block; font-size: 20px; font-weight: 700; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.short-debt-amount small { font-size: 12px; }
.short-debt-card :deep(.short-pnl) { padding: 6px 0; }
.short-debt-card :deep(.short-pnl-amount) { margin: 2px 0; font-size: 18px; }
.short-debt-card :deep(.short-pnl-note) { font-size: 11px; line-height: 1.5; }
.short-details { font-size: 11px; }
.short-details > div, .rule-metrics > div { display: flex; justify-content: space-between; gap: 8px; padding: 5px 0; border-top: 1px solid #ddd; }
.short-details dt, .rule-metrics dt { color: #666; min-width: 0; }
.short-details dd, .rule-metrics dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; min-width: 0; }
.position-warning { font-size: 12px; margin-top: 6px; }
.cover-link { display: block; background: #000; color: #fff; text-decoration: none; padding: 6px 10px; margin-top: 8px; text-align: center; font-size: 13px; font-weight: 700; }
.cover-link:hover { background: #333; }
.position-details { margin-top: 8px; color: #666; font-size: 11px; }
.position-details p { margin-top: 6px; }
.short-empty { margin-top: 10px; }
.short-note { margin-top: 10px; }
.rule-details { margin-top: 8px; border-top: 1px solid #ddd; font-size: 12px; }
.rule-details > summary { padding: 8px 0; font-weight: 700; }
summary { cursor: pointer; }
.rule-content { color: #555; padding-bottom: 8px; max-width: 80ch; }
.rule-content p + p { margin-top: 8px; }
.rule-metrics { margin: 4px 0 8px; }
.rule-metrics dd { font-weight: 700; }
#gold-loan, #short-debt, #loan-rules { scroll-margin-top: 90px; }
.cover-link:focus-visible, .details-link:focus-visible, summary:focus-visible { outline: 2px solid #000; outline-offset: 3px; }

@media (max-width: 1000px) {
  .loan-workbench { grid-template-columns: minmax(0, 1fr); }
  .short-debt-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 640px) {
  .loan-page { padding: 4px; }
  .loan-header h1 { font-size: 20px; }
  .loan-actions, .short-debt-list { grid-template-columns: minmax(0, 1fr); }
  .loan-actions { gap: 12px; }
  .loan-action + .loan-action { border-top: 1px solid #ddd; padding-top: 12px; }
  .loan-stats > div { padding: 0 6px; }
  .loan-stats dd { font-size: 17px; }
  .loan-stats dt { font-size: 11px; }
  .amount-row :deep(.n-button), .repay-all { min-height: 44px; }
  .amount-input :deep(.n-input) { --n-height: 44px !important; }
  .amount-input :deep(.n-input__input-el) { font-size: 16px; }
  .quick-amounts :deep(.n-button) { min-height: 32px; padding: 0 10px; }
  .cover-link { min-height: 44px; display: flex; align-items: center; justify-content: center; }
  .rule-details > summary { min-height: 44px; }
}
</style>
