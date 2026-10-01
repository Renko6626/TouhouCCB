<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { NInputNumber, NButton, NSpin, NAlert, NDivider, useMessage } from 'naive-ui'
import { useLoanStore } from '@/stores/loan'
import { fetchLiquidationPolicy, type LiquidationPolicy } from '@/api/loan'
import { extractErrorMessage } from '@/utils/errors'
import { compareFxAmounts, divideFxAmount, formatFxAmount, subtractFxAmounts } from '@/api/fx'
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

const policyHardPct = computed(() =>
  policy.value ? (policy.value.hard_threshold * 100).toFixed(0) : '—')
const policyEmergencyPct = computed(() =>
  policy.value ? (policy.value.emergency_threshold * 100).toFixed(0) : '—')
const policyTargetPct = computed(() =>
  policy.value ? (policy.value.target_margin * 100).toFixed(0) : '—')
const policyPartialPct = computed(() =>
  policy.value ? (policy.value.partial_pct * 100).toFixed(0) : '—')
const policyInterval = computed(() => policy.value?.sweep_interval_sec ?? '—')
const policyEnabled = computed(() => policy.value?.enabled ?? false)

const dailyRatePct = computed(() => {
  const r = store.quota?.daily_rate
  if (!r) return '—'
  return (Number(r) * 100).toFixed(2) + '%'
})

const shortPositions = computed(() => store.quota?.short_positions ?? [])
const marginRatio = computed(() => {
  if (store.quota?.equity_to_risk_basis != null) return store.quota.equity_to_risk_basis
  if (policy.value?.unified_credit_enabled !== false || store.quota?.net_worth == null) return null
  const ratio = divideFxAmount(store.quota.net_worth, store.quota.debt, 12)
  return ratio == null ? null : Number(ratio)
})
function pendingInterest(position: AccountShortPosition) {
  if (position.pending_short_debt == null) return null
  const interest = subtractFxAmounts(position.pending_short_debt, position.principal_foreign)
  return interest == null ? null : subtractFxAmounts(interest, position.interest_foreign)
}

const debtNumber = computed(() => Number(store.quota?.debt ?? '0'))
const cashNumber = computed(() => Number(store.quota?.available_cash ?? (policy.value?.unified_credit_enabled === false ? store.quota?.cash ?? '0' : '0')))
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

async function submitBorrow() {
  if (busy.value || !borrowAmount.value || borrowAmount.value <= 0) return
  submitting.value = true
  try {
    await store.borrow(String(borrowAmount.value))
    msg.success(`借入 ${borrowAmount.value}`)
    borrowAmount.value = null
  } catch (e: unknown) {
    msg.error(extractErrorMessage(e, '借款失败'))
  } finally {
    submitting.value = false
  }
}

async function submitRepay() {
  if (busy.value || !repayAmount.value || repayAmount.value <= 0 || repayAmount.value > maxRepayNumber.value) return
  submitting.value = true
  try {
    const r = await store.repay(String(repayAmount.value))
    const eff = r.effective ? Number(r.effective) : Number(repayAmount.value)
    if (Math.abs(eff - Number(repayAmount.value)) > 0.001) {
      msg.success(`实际还款 金 ${eff.toFixed(2)}（输入 金 ${repayAmount.value} 已自动按真实负债 / 现金封顶）`)
    } else {
      msg.success(`还款 金 ${eff.toFixed(2)}`)
    }
    repayAmount.value = null
  } catch (e: unknown) {
    msg.error(extractErrorMessage(e, '还款失败'))
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
    msg.error(extractErrorMessage(e, '还款失败'))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="loan-page">
    <header class="loan-header">
      <div>
        <span class="loan-eyebrow">我的负债</span>
        <h1>贷款与空头</h1>
        <p>金圆券借款用金还款，外币空头买回对应外币归还。</p>
      </div>
      <NButton :disabled="busy" @click="store.refresh">刷新负债</NButton>
    </header>
    <NSpin :show="store.loading">
      <NAlert v-if="store.error" type="error" :title="store.error" />
      <NAlert
        v-else-if="store.quota && !store.quota.enabled"
        type="warning"
        title="借款功能维护中"
      />

      <div class="debt-overview">
        <a href="#gold-loan" class="debt-overview-card">
          <span class="overview-label">金圆券借款 · 含息</span>
          <strong :class="{ red: debtNumber > 0 }">金 {{ formatFxAmount(store.quota?.debt, 2) }}</strong>
          <span>还可借入 金 {{ formatFxAmount(store.quota?.max_borrow, 2) }}</span>
          <span class="overview-link">借款 / 还款 ↓</span>
        </a>
        <a href="#short-debt" class="debt-overview-card short-overview">
          <span class="overview-label">外币空头负债</span>
          <strong>{{ store.quota ? shortPositions.length : '—' }} <small>笔待回补</small></strong>
          <span>全仓参考回补成本 金 {{ formatFxAmount(store.quota?.short_cover_cost, 2) }}</span>
          <span class="overview-link">查看欠币与回补 ↓</span>
        </a>
        <div class="debt-overview-card">
          <CreditRiskStatus
            :ratio="marginRatio"
            :initial="store.quota?.r_initial ?? policy?.r_initial ?? (policy?.unified_credit_enabled === false ? policy.soft_threshold : null)"
            :maintenance="store.quota?.r_maintenance ?? policy?.r_maintenance ?? (policy?.unified_credit_enabled === false ? policy.hard_threshold : null)"
            :blocked="store.quota?.risk_status === 'blocked'"
            :legacy="policy?.unified_credit_enabled === false"
            :no-risk="compareFxAmounts(store.quota?.risk_basis, '0') === 0"
          />
          <span>未锁定现金 金 {{ formatFxAmount(store.quota?.available_cash, 2) }}</span>
          <span>锁定空头所得 金 {{ formatFxAmount(store.quota?.restricted_cash, 2) }}</span>
        </div>
      </div>

      <section id="short-debt" class="panel short-debt-panel" aria-labelledby="short-debt-heading">
        <div class="section-heading">
          <div>
            <h2 id="short-debt-heading">外币空头贷款</h2>
            <p class="section-intro">这里是你借入并卖出的外币。欠币包含利息，需买回对应外币归还。</p>
          </div>
          <span class="short-count">{{ store.quota ? shortPositions.length : '—' }} 笔空头</span>
        </div>
        <p v-if="!store.quota" class="short-empty">{{ store.error ? '负债读取失败，请刷新后查看空头。' : '正在读取空头负债…' }}</p>
        <div v-else-if="shortPositions.length" class="short-debt-list">
          <article v-for="position in shortPositions" :key="position.pair_id" class="short-debt-card">
            <div class="section-heading">
              <h3>{{ position.currency_code }} <span class="short-tag">空头</span></h3>
              <span class="position-status" :class="{ blocked: !position.executable }">{{ position.executable ? '可获取回补报价' : '回补受限' }}</span>
            </div>
            <div class="short-debt-amount">
              <span>待归还 · 含待计利息</span>
              <strong>{{ formatFxAmount(position.pending_short_debt, 6) }} <small>{{ position.currency_code }}</small></strong>
            </div>
            <ShortPositionPnl
              :proceeds-basis-gold="position.proceeds_basis_gold"
              :reference-cover-cost="position.reference_cover_cost"
            />
            <dl class="short-details">
              <div><dt>借入本金</dt><dd>{{ formatFxAmount(position.principal_foreign, 6) }} {{ position.currency_code }}</dd></div>
              <div><dt>已结利息 / 待计利息</dt><dd>{{ formatFxAmount(position.interest_foreign, 6) }} / {{ formatFxAmount(pendingInterest(position), 6) }} {{ position.currency_code }}</dd></div>
              <div><dt>本仓锁定所得</dt><dd>金 {{ formatFxAmount(position.restricted_gold) }}</dd></div>
              <div><dt>全仓参考回补成本</dt><dd>{{ position.reference_cover_cost == null ? '估值待恢复' : `金 ${formatFxAmount(position.reference_cover_cost)}` }}</dd></div>
              <div><dt>参考回补手续费（已含）</dt><dd>金 {{ formatFxAmount(position.reference_cover_fee) }}</dd></div>
            </dl>
            <p v-if="!position.executable || position.blocked_reason" class="position-warning">{{ position.blocked_reason || '暂无法执行全仓回补，请在交易面板查看具体报价。' }}</p>
            <div class="short-card-footer">
              <span>回补成本含息、手续费与滑点，以成交报价为准。</span>
              <router-link :to="{ path: '/fx', query: { pair: position.pair_id, action: 'cover' } }" class="cover-link">买回归还 {{ position.currency_code }} →</router-link>
            </div>
          </article>
        </div>
        <div v-else class="short-empty">
          <strong>暂无待回补的外币空头</strong>
          <p>开空后，借入的外币和待归还利息会集中显示在这里。</p>
          <router-link :to="{ path: '/fx', query: { action: 'open' } }">前往外汇做空 →</router-link>
        </div>
        <p class="short-note">空头所得已包含在总现金中，锁定用于本仓回补，不能用于消费或偿还金圆券借款。全部资产共享保证金。</p>
      </section>

      <section v-if="policy" class="panel liq-panel">
        <h2>{{ policy.unified_credit_enabled ? '统一信贷：预测市场与外汇共用额度' : '预测市场借款' }}</h2>
        <template v-if="policy.unified_credit_enabled">
          <p class="liq-intro">
            借入的是金圆券，可用于预测市场和外汇交易。现金、预测市场持仓及各外汇持仓共同支持授信，外币回补义务也占用额度，
            金圆券借款和外币空头负债在此统一查看。
          </p>
          <p v-if="policy.credit_leverage != null" class="liq-intro">
            当前名义杠杆上限 <strong>{{ policy.credit_leverage }}x</strong>，
            新增借款时，金圆券借款上限还需扣除外币回补风险占用；无空头时，借款不得超过清算净值的 <strong>{{ Number((policy.credit_leverage - 1).toFixed(6)) }} 倍</strong>。
            这是授信上限，借款不会自动买入持仓，也不代表你当前已使用这个倍数。
          </p>
          <p class="liq-intro">
            任一持仓亏损都会影响共用额度；触发强平时，其他预测市场或外汇持仓也可能被卖出还债。
            自持外币需先卖成金圆券才能偿还金债；欠币可从上方空头卡片进入外汇右侧面板回补。
          </p>
        </template>
        <p v-else class="liq-intro">
          当前未开启统一信贷，FX 持仓不计入借款抵押；有未还借款时不能买入外币。
          借款和还款均使用金圆券。
        </p>
      </section>
      <NAlert v-else :type="policyError ? 'warning' : 'info'" :title="policyError ? '借款规则暂时加载失败' : '正在读取当前借款规则'">
        {{ policyError ? '暂无法确认当前杠杆与强平规则，请刷新页面重试。' : '模式、杠杆及强平说明以服务器当前生效配置为准。' }}
      </NAlert>

      <section id="gold-loan" class="panel">
        <h2>金圆券借款（含息）</h2>
        <div class="debt-number" :class="{ red: debtNumber > 0 }">
          {{ store.quota?.debt ?? '—' }}
        </div>
        <div class="meta">
          <span>还可借入：<strong>{{ store.quota?.max_borrow ?? '—' }}</strong></span>
          <span class="sep">·</span>
          <span>日利率：<strong>{{ dailyRatePct }}</strong></span>
          <span class="sep">·</span>
          <span>总现金：<strong>{{ store.quota?.cash ?? '—' }}</strong></span>
        </div>
        <div class="meta-small" v-if="store.quota?.last_accrued_at">
          上次结息：{{ new Date(store.quota.last_accrued_at).toLocaleString() }}
        </div>
      </section>

      <section v-if="policy?.unified_credit_enabled && store.quota" class="panel">
        <h3>现金用途与外币风险</h3>
        <p>未锁定现金：金 {{ store.quota.available_cash ?? '—' }}；锁定空头所得：金 {{ store.quota.restricted_cash ?? '—' }}（已包含在总现金中，专用于回补）。</p>
        <p>全仓回补成本 K：金 {{ store.quota.short_cover_cost ?? '—' }}；风险基数 B：金 {{ store.quota.risk_basis ?? '—' }}；保证金率 E/B：{{ store.quota.equity_to_risk_basis?.toFixed(3) ?? '—' }}。</p>
        <NAlert v-if="store.quota.risk_status === 'blocked'" type="warning" title="风险检查阻塞">
          {{ store.quota.blocked_reason || '估值待恢复，新增风险暂不可用。' }}
        </NAlert>
      </section>

      <NDivider />

      <section class="panel">
        <h3>借款</h3>
        <div class="row">
          <NInputNumber
            v-model:value="borrowAmount"
            placeholder="金额"
            :min="0.01"
            :max="maxBorrowNumber"
            :precision="2"
            :disabled="busy || !store.quota?.enabled || maxBorrowNumber <= 0"
            style="width: 200px"
          />
          <NButton
            type="primary"
            :loading="submitting"
            :disabled="busy || !store.quota?.enabled || !borrowAmount || borrowAmount <= 0"
            @click="submitBorrow"
          >借入</NButton>
        </div>
        <div v-if="store.quota?.enabled" class="meta-small">
          还可借入：<strong>金 {{ maxBorrowNumber.toFixed(2) }}</strong>
        </div>
        <p v-if="policy?.unified_credit_enabled" class="meta-small">
          额度按清算净值和共享风险基数计算，已扣除已有金债与外币回补占用。
          行情、手续费和滑点都会影响额度；借入和买入时会重新检查，借满后也可能因交易成本而无法继续买入。
        </p>
      </section>

      <section class="panel">
        <h3>还款</h3>
        <div class="row">
          <NInputNumber
            v-model:value="repayAmount"
            placeholder="金额"
            :min="0.01"
            :precision="2"
            :max="maxRepayNumber"
            :disabled="busy || debtNumber <= 0 || cashNumber <= 0"
            style="width: 200px"
          />
          <NButton
            :loading="submitting"
            :disabled="busy || !repayAmount || repayAmount <= 0 || debtNumber <= 0 || cashNumber <= 0 || repayAmount > maxRepayNumber"
            @click="submitRepay"
          >还款</NButton>
          <NButton
            quaternary
            :loading="submitting"
            :disabled="busy || !store.quota || debtNumber <= 0 || cashNumber <= 0"
            @click="repayAll"
          >{{ cashNumber >= debtNumber ? '全部还清' : '用可用现金还款' }}</NButton>
        </div>
        <p class="meta-small">
          “全部还清 / 用可用现金还款”会按提交时的最新借款计息后还款；现金不足时仍会保留欠款。
        </p>
        <div v-if="repayOverflow > 0" class="meta-small warn">
          <span class="warning-tag">注意</span>
          输入 金 {{ repayAmount }} 超过当前可还上限 金 {{ maxRepayNumber.toFixed(6) }}，
          实际只会按提交时的金圆券借款 / 未锁定现金封顶（多出的金额不收取）
        </div>
        <div v-else-if="debtNumber > 0" class="meta-small">
          当前借款（含利息）<strong>金 {{ debtNumber.toFixed(6) }}</strong>，可用现金 <strong>金 {{ cashNumber.toFixed(6) }}</strong>，
          当前可还上限 <strong>金 {{ maxRepayNumber.toFixed(6) }}</strong>
        </div>
      </section>

      <NDivider />

      <section v-if="policy?.unified_credit_enabled" class="panel liq-panel">
        <h3>跨产品定时强平</h3>
        <p>账面净值：金 {{ store.quota?.display_equity ?? '—' }}；清算净值：金 {{ store.quota?.liquidation_equity ?? '—' }}。</p>
        <p>清算净值 E = 总现金 + 预测市场与外汇多头实际可变现金额 − 含息金债 D − 外币全仓回补成本 K。
          不可卖资产的清算价值为 0；无法完整报价的外币负债不能按 0 处理，会阻塞风险检查。</p>
        <p>初始 / 恢复率 {{ ((policy.r_initial ?? 0) * 100).toFixed(2) }}%；
          维持率 {{ ((policy.r_maintenance ?? 0) * 100).toFixed(2) }}%。</p>
        <p>每 {{ policy.sweep_interval_sec }} 秒扫描；清算净值 E ÷ 风险基数 B 低于维持率时触发。
          系统选择一组资产卖出或外币义务回补；有待回补义务时保留现金用于回补。
          每人每次扫描最多处理一组，通常处理该组的 {{ (policy.partial_pct * 100).toFixed(2) }}%；清算净值不大于 0 时扩大减仓，回补仍受实际现金与市场容量限制。
          下一次扫描继续，恢复到初始率或还清即停止；卖光仍欠债会冻结新增信用，不会免债。</p>
        <p>强平不另收罚金：预测市场卖出费率 {{ ((policy.sell_fee_rate ?? 0) * 100).toFixed(2) }}%。
          <span v-for="pair in policy.fx_sell_fee_rates" :key="pair.pair_id">
            {{ pair.currency_code }} 卖出费率 {{ (pair.sell_fee_rate * 100).toFixed(2) }}%。
          </span>
        </p>
        <p>机制{{ policy.enabled ? '已开启' : '已暂停' }}。交易或行情变化不会额外触发强平。</p>
      </section>
      <section v-else-if="policy" class="panel liq-panel">
        <h3>强制平仓机制</h3>
        <p class="liq-intro">
          有负债时，系统会按下面规则定期检查你的保证金率
          <code class="liq-formula">（现金 + LCV 持仓清算价值 − 负债）÷ 负债</code>。
          跌破触发线就会自动卖出部分持仓还债，<strong>不需要也无法手动取消</strong>。
        </p>

        <div class="liq-grid">
          <div class="liq-cell">
            <div class="liq-cell-label">触发线（hard）</div>
            <div class="liq-cell-value liq-danger">&lt; {{ policyHardPct }}%</div>
            <div class="liq-cell-hint">保证金率跌破此线启动强平</div>
          </div>
          <div class="liq-cell">
            <div class="liq-cell-label">紧急升级线</div>
            <div class="liq-cell-value liq-danger">&lt; {{ policyEmergencyPct }}%</div>
            <div class="liq-cell-hint">跌破此线一次性<strong>全平所有持仓</strong></div>
          </div>
          <div class="liq-cell">
            <div class="liq-cell-label">收敛目标</div>
            <div class="liq-cell-value">≥ {{ policyTargetPct }}%</div>
            <div class="liq-cell-hint">渐进卖到此值即停手</div>
          </div>
          <div class="liq-cell">
            <div class="liq-cell-label">每波卖出比例</div>
            <div class="liq-cell-value">{{ policyPartialPct }}%</div>
            <div class="liq-cell-hint">每 tick 卖每仓 {{ policyPartialPct }}%</div>
          </div>
          <div class="liq-cell">
            <div class="liq-cell-label">扫描频率</div>
            <div class="liq-cell-value">{{ policyInterval }} 秒</div>
            <div class="liq-cell-hint">scheduler 周期性扫描</div>
          </div>
          <div class="liq-cell">
            <div class="liq-cell-label">机制状态</div>
            <div class="liq-cell-value" :class="policyEnabled ? 'liq-on' : 'liq-off'">
              {{ policyEnabled ? '已开启' : '已暂停' }}
            </div>
            <div class="liq-cell-hint">管理员可临时关停</div>
          </div>
        </div>

        <div class="liq-flow">
          <span class="liq-step">保证金率 &lt; {{ policyHardPct }}%</span>
          <span class="liq-arrow">→</span>
          <span class="liq-step">每 {{ policyInterval }}s 卖 {{ policyPartialPct }}% 还债</span>
          <span class="liq-arrow">→</span>
          <span class="liq-step">回到 {{ policyTargetPct }}% 停手</span>
        </div>
        <div class="liq-flow liq-flow-emergency">
          <span class="liq-step liq-step-danger">保证金率 &lt; {{ policyEmergencyPct }}%</span>
          <span class="liq-arrow">→</span>
          <span class="liq-step liq-step-danger">一次性全平所有持仓</span>
        </div>

        <div class="liq-tips">
          <div class="liq-tip">
            <span class="liq-tip-tag">提示</span>
            想避免被强平：减小杠杆 · 主动还款 · 关注重仓 outcome 的价格波动。
          </div>
          <div class="liq-tip">
            <span class="liq-tip-tag">注意</span>
            LCV 口径已扣手续费与滑点，所以保证金率会比 Portfolio 顶部账面净值略低，这是设计。
          </div>
        </div>
      </section>
    </NSpin>
  </div>
</template>

<style scoped>
.loan-page {
  padding: 16px;
  max-width: 1040px;
  margin: 0 auto;
}
.panel {
  margin-bottom: 16px;
  border: 2px solid #000;
  padding: 16px;
  background: #fff;
}
.debt-number {
  font-size: 40px;
  font-weight: 700;
}
.debt-number.red, .red {
  color: var(--color-down);
}
.meta {
  margin-top: 8px;
  color: #555;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 8px;
  align-items: baseline;
}
.sep {
  color: #bbb;
}
.meta-small {
  margin-top: 4px;
  font-size: 12px;
  color: #888;
}
.meta-small.warn {
  color: #b45309;
  font-weight: 600;
}
.warning-tag {
  display: inline-block;
  padding: 0 6px;
  margin-right: 4px;
  border: 1.5px solid #b45309;
  background: #b45309;
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  vertical-align: 1px;
}
.meta-small strong {
  color: #000;
}
.row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
h2, h3 {
  margin: 0 0 8px 0;
}

.loan-header, .section-heading, .short-card-footer { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.loan-header { margin-bottom: 20px; }
.loan-header h1 { margin: 4px 0 8px; font-size: 28px; font-weight: 800; }
.loan-header p, .section-intro { margin: 0; font-size: 13px; color: #555; line-height: 1.6; }
.loan-eyebrow { font-size: 12px; font-weight: 700; letter-spacing: 0.1em; }
.debt-overview { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-bottom: 20px; }
.debt-overview-card { display: flex; flex-direction: column; gap: 8px; border: 2px solid #000; padding: 16px; color: #000; background: #fff; text-decoration: none; font-size: 12px; }
a.debt-overview-card:hover { box-shadow: 3px 3px 0 #000; }
.overview-label { font-weight: 700; }
.debt-overview-card > strong { font-size: 26px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.debt-overview-card small { font-size: 13px; }
.overview-link { margin-top: auto; font-weight: 700; text-decoration: underline; text-underline-offset: 3px; }
.short-overview { background: #000; color: #fff; border-top: 6px solid #000; padding-top: 12px; }
.short-debt-panel { border-top: 6px solid #000; }
#short-debt, #gold-loan { scroll-margin-top: 90px; }
.short-count, .short-tag { border: 1px solid #000; background: #000; color: #fff; padding: 3px 8px; font-size: 12px; font-weight: 700; }
.short-tag { vertical-align: middle; margin-left: 6px; }
.short-debt-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 16px; }
.short-debt-card { border: 2px solid #000; padding: 16px; background: #fafafa; min-width: 0; }
.short-debt-card h3 { margin: 0; font-size: 18px; }
.position-status { color: #555; font-size: 12px; }
.position-status.blocked, .position-warning { color: #b45309; }
.short-debt-amount { display: flex; flex-direction: column; gap: 4px; margin: 16px 0; }
.short-debt-amount > span { color: #555; font-size: 12px; }
.short-debt-amount strong { font-size: 24px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.short-debt-amount small { font-size: 14px; }
.short-details { margin: 0; font-size: 12px; }
.short-details > div { display: flex; justify-content: space-between; gap: 12px; padding: 8px 0; border-top: 1px solid #ddd; }
.short-details dt { color: #555; flex-shrink: 0; }
.short-details dd { margin: 0; text-align: right; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
.position-warning { font-size: 12px; line-height: 1.6; }
.short-card-footer { margin-top: 12px; padding-top: 12px; border-top: 1px solid #000; }
.short-card-footer > span { font-size: 12px; color: #666; }
.cover-link { display: inline-block; padding: 9px 12px; background: #000; color: #fff; text-decoration: none; font-weight: 700; font-size: 13px; }
.cover-link:hover { background: #333; }
.short-empty { margin-top: 16px; padding: 20px; border: 1px dashed #999; background: #fafafa; font-size: 13px; line-height: 1.7; }
.short-empty a { color: #000; font-weight: 700; text-underline-offset: 3px; }
.short-note { margin: 16px 0 0; color: #555; font-size: 12px; line-height: 1.6; }
@media (max-width: 700px) {
  .debt-overview, .short-debt-list { grid-template-columns: 1fr; }
  .loan-page { padding: 12px; }
  .short-debt-card { padding: 12px; }
  .cover-link { width: 100%; text-align: center; }
}

/* ── 强制平仓说明 panel ───────────────────────────── */
.liq-panel {
  background: #fafafa;
}
.liq-intro {
  margin: 0 0 12px 0;
  font-size: 13px;
  line-height: 1.6;
  color: #333;
}
.liq-formula {
  display: inline-block;
  padding: 1px 6px;
  background: #fff;
  border: 1.5px solid #000;
  font-size: 12px;
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-weight: 600;
}
.liq-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  border: 2px solid #000;
  margin-bottom: 14px;
}
.liq-cell {
  padding: 10px 12px;
  border-right: 1px solid #000;
  border-bottom: 1px solid #000;
  background: #fff;
}
.liq-cell:nth-child(3n) { border-right: none; }
.liq-cell:nth-last-child(-n+3) { border-bottom: none; }
.liq-cell-label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  color: #666;
  text-transform: uppercase;
  margin-bottom: 4px;
}
.liq-cell-value {
  font-size: 18px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  color: #000;
  margin-bottom: 2px;
}
.liq-cell-hint {
  font-size: 11px;
  color: #777;
  line-height: 1.4;
}
.liq-cell-hint strong { color: #000; }
.liq-danger { color: var(--color-down, #cc0000); }
.liq-on { color: var(--color-up, #1a8a3a); }
.liq-off { color: #888; }

/* 流程图 */
.liq-flow {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border: 2px solid #000;
  background: #fff;
  margin-bottom: 6px;
}
.liq-flow-emergency {
  background: #fff5f5;
  border-color: var(--color-down, #cc0000);
}
.liq-step {
  font-size: 12px;
  font-weight: 700;
  padding: 4px 8px;
  background: #f0f0f0;
  border: 1.5px solid #000;
}
.liq-step-danger {
  background: var(--color-down, #cc0000);
  color: #fff;
  border-color: var(--color-down, #cc0000);
}
.liq-arrow {
  font-size: 16px;
  font-weight: 900;
  color: #000;
}

/* 提示 */
.liq-tips {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.liq-tip {
  font-size: 12px;
  color: #444;
  line-height: 1.5;
}
.liq-tip-tag {
  display: inline-block;
  padding: 0 6px;
  margin-right: 6px;
  border: 1.5px solid #000;
  background: #000;
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  vertical-align: 1px;
}

@media (max-width: 640px) {
  .liq-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .liq-cell:nth-child(3n) { border-right: 1px solid #000; }
  .liq-cell:nth-child(2n) { border-right: none; }
  .liq-cell:nth-last-child(-n+3) { border-bottom: 1px solid #000; }
  .liq-cell:nth-last-child(-n+2) { border-bottom: none; }
}
</style>
