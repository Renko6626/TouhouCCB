<script setup lang="ts">
import { ref, watch } from 'vue'
import { NModal } from 'naive-ui'
import { compareFxAmounts, formatFxAmount, multiplyFxAmount } from '@/api/fx'
import { fxMarginPercent } from '@/utils/fxPresentation'
import type { FxBorrowConfirmationDetails } from '@/composables/useFxBorrowConfirmation'
const props = defineProps<{ details: FxBorrowConfirmationDetails | null }>()
const emit = defineEmits<{ confirm: [dontShowAgain: boolean]; cancel: [] }>()
const dontShowAgain = ref(false)
watch(
  () => props.details,
  () => {
    dontShowAgain.value = false
  },
)
</script>
<template>
  <NModal
    v-if="details"
    :show="!!details"
    @update:show="
      (value) => {
        if (!value) emit('cancel')
      }
    "
  >
    <div
      class="borrow-confirmation"
      role="dialog"
      aria-modal="true"
      aria-labelledby="borrow-confirmation-title"
    >
      <h2 id="borrow-confirmation-title">
        {{ details.kind === 'long' ? '确认借款买入' : '确认借币做空' }}
      </h2>
      <template v-if="details.kind === 'long'">
        <p>
          本次买入需要借入
          <strong>{{ formatFxAmount(details.borrowAmount, 6) }} 金圆券</strong
          >，借款将用于这笔交易。
        </p>
        <dl>
          <div>
            <dt>现金投入</dt>
            <dd>{{ formatFxAmount(details.cashInput, 6) }} 金圆券</dd>
          </div>
          <div v-if="compareFxAmounts(details.lockedGold, '0') === 1">
            <dt>本仓锁金投入</dt>
            <dd>{{ formatFxAmount(details.lockedGold, 6) }} 金圆券</dd>
          </div>
          <div>
            <dt>预计买入</dt>
            <dd>{{ formatFxAmount(details.quantity, 6) }} {{ details.currency }}</dd>
          </div>
        </dl>
        <p>未还借款会产生利息。价格下跌可能造成亏损。</p>
      </template>
      <template v-else>
        <p>
          本次将借入并卖出
          <strong>{{ formatFxAmount(details.borrowAmount, 6) }} {{ details.currency }}</strong
          >。
        </p>
        <dl>
          <div>
            <dt>预计卖出所得锁定</dt>
            <dd>{{ formatFxAmount(details.lockedGold, 6) }} 金圆券</dd>
          </div>
        </dl>
        <p>
          锁定所得用于回补，不能花用。你需要买回并归还借入的外币及利息；价格上涨会增加回补成本。
        </p>
      </template>
      <div class="borrow-risk">
        <div>
          <span>预计成交后保证金率</span
          ><strong>{{
            details.reliable ? fxMarginPercent(details.equity, details.basis) : '暂无法可靠估算'
          }}</strong>
        </div>
        <p v-if="details.maintenance != null">
          低于
          <strong>{{ formatFxAmount(multiplyFxAmount(details.maintenance, '100'), 2) }}%</strong>
          时会触发强平。
        </p>
        <p v-else>维持门槛暂不可确认。</p>
        <small>按全账户持仓估算，实际以成交时检查为准。</small>
      </div>
      <label class="borrow-opt-out"
        ><input v-model="dontShowAgain" type="checkbox" />以后不再显示借款交易确认</label
      >
      <p class="borrow-preference-note">可在个人页面重新开启，仅对当前浏览器生效。</p>
      <div class="borrow-actions">
        <button type="button" @click="emit('cancel')">返回修改</button>
        <button type="button" class="borrow-confirm" @click="emit('confirm', dontShowAgain)">
          {{ details.kind === 'long' ? '确认借款并买入' : '确认借币并做空' }}
        </button>
      </div>
    </div>
  </NModal>
</template>
<style scoped>
.borrow-confirmation {
  width: min(460px, calc(100vw - 32px));
  box-sizing: border-box;
  max-height: calc(100dvh - 32px);
  overflow-y: auto;
  background: #fff;
  color: #000;
  border: 2px solid #000;
  padding: 24px;
}
h2 {
  margin: 0 0 18px;
  font-size: 20px;
}
p {
  margin: 12px 0;
  font-size: 13px;
  line-height: 1.7;
}
dl {
  margin: 16px 0;
}
dl div,
.borrow-risk > div {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  font-size: 13px;
}
dt {
  color: #666;
}
dd {
  margin: 0;
  text-align: right;
  font-weight: 650;
}
.borrow-risk {
  margin: 20px 0;
  padding: 14px 0;
  border-block: 1px solid #ddd;
}
.borrow-risk p {
  margin: 8px 0 4px;
}
small,
.borrow-preference-note {
  color: #777;
  font-size: 11px;
}
.borrow-opt-out {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.borrow-preference-note {
  margin: 4px 0 20px;
}
.borrow-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
button {
  min-height: 42px;
  padding: 8px 14px;
  border: 2px solid #000;
  background: #fff;
  color: #000;
  cursor: pointer;
  font: inherit;
  font-size: 13px;
}
.borrow-confirm {
  background: #000;
  color: #fff;
  font-weight: 650;
}
button:focus-visible {
  outline: 2px solid #000;
  outline-offset: 3px;
}
</style>
