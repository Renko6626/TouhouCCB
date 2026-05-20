<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { NInputNumber, NButton, NSpin, NAlert, NDivider, useMessage } from 'naive-ui'
import { useLoanStore } from '@/stores/loan'
import { fetchLiquidationPolicy, type LiquidationPolicy } from '@/api/loan'

const store = useLoanStore()
const msg = useMessage()

const borrowAmount = ref<number | null>(null)
const repayAmount = ref<number | null>(null)
const submitting = ref(false)

const policy = ref<LiquidationPolicy | null>(null)

onMounted(async () => {
  store.refresh()
  try {
    policy.value = await fetchLiquidationPolicy()
  } catch {
    // policy 拉失败不阻塞主流程；说明卡只是教育用
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

const debtNumber = computed(() => Number(store.quota?.debt ?? '0'))
const cashNumber = computed(() => Number(store.quota?.cash ?? '0'))
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
  if (!borrowAmount.value || borrowAmount.value <= 0) return
  submitting.value = true
  try {
    await store.borrow(String(borrowAmount.value))
    msg.success(`借入 ${borrowAmount.value}`)
    borrowAmount.value = null
  } catch (e: any) {
    msg.error(e?.data?.detail ?? e?.message ?? '借款失败')
  } finally {
    submitting.value = false
  }
}

async function submitRepay() {
  if (!repayAmount.value || repayAmount.value <= 0) return
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
  } catch (e: any) {
    msg.error(e?.data?.detail ?? e?.message ?? '还款失败')
  } finally {
    submitting.value = false
  }
}

function repayAll() {
  // 还到能还的最大值：min(真实负债, 真实现金)
  if (maxRepayNumber.value <= 0) return
  repayAmount.value = maxRepayNumber.value
  submitRepay()
}
</script>

<template>
  <div class="loan-page">
    <NSpin :show="store.loading">
      <NAlert v-if="store.error" type="error" :title="store.error" />
      <NAlert
        v-else-if="store.quota && !store.quota.enabled"
        type="warning"
        title="借款功能维护中"
      />

      <section class="panel">
        <h2>当前负债</h2>
        <div class="debt-number" :class="{ red: debtNumber > 0 }">
          {{ store.quota?.debt ?? '—' }}
        </div>
        <div class="meta">
          <span>可借额度：<strong>{{ store.quota?.max_borrow ?? '—' }}</strong></span>
          <span class="sep">·</span>
          <span>日利率：<strong>{{ dailyRatePct }}</strong></span>
          <span class="sep">·</span>
          <span>现金：<strong>{{ store.quota?.cash ?? '—' }}</strong></span>
        </div>
        <div class="meta-small" v-if="store.quota?.last_accrued_at">
          上次结息：{{ new Date(store.quota.last_accrued_at).toLocaleString() }}
        </div>
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
            :disabled="!store.quota?.enabled || maxBorrowNumber <= 0"
            style="width: 200px"
          />
          <NButton
            type="primary"
            :loading="submitting"
            :disabled="!store.quota?.enabled || !borrowAmount || borrowAmount <= 0"
            @click="submitBorrow"
          >借入</NButton>
        </div>
        <div v-if="store.quota?.enabled" class="meta-small">
          可借额度：<strong>金 {{ maxBorrowNumber.toFixed(2) }}</strong>
        </div>
      </section>

      <section class="panel">
        <h3>还款</h3>
        <div class="row">
          <NInputNumber
            v-model:value="repayAmount"
            placeholder="金额"
            :min="0.01"
            :precision="2"
            :disabled="debtNumber <= 0 || cashNumber <= 0"
            style="width: 200px"
          />
          <NButton
            :loading="submitting"
            :disabled="!repayAmount || repayAmount <= 0 || debtNumber <= 0"
            @click="submitRepay"
          >还款</NButton>
          <NButton
            quaternary
            :disabled="maxRepayNumber <= 0"
            @click="repayAll"
          >还到上限 金 {{ maxRepayNumber.toFixed(2) }}</NButton>
        </div>
        <div v-if="repayOverflow > 0" class="meta-small warn">
          <span class="warning-tag">注意</span>
          输入 金 {{ repayAmount }} 超过可还上限 金 {{ maxRepayNumber.toFixed(2) }}，
          实际只会扣减 金 {{ maxRepayNumber.toFixed(2) }}（多出的 金 {{ repayOverflow.toFixed(2) }} 不收取）
        </div>
        <div v-else-if="debtNumber > 0" class="meta-small">
          当前真实负债 <strong>金 {{ debtNumber.toFixed(2) }}</strong>，可用现金 <strong>金 {{ cashNumber.toFixed(2) }}</strong>，
          可还上限 <strong>金 {{ maxRepayNumber.toFixed(2) }}</strong>
        </div>
      </section>

      <NDivider />

      <section class="panel liq-panel">
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
  max-width: 640px;
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
.debt-number.red {
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
  gap: 8px;
  align-items: center;
}
h2, h3 {
  margin: 0 0 8px 0;
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
