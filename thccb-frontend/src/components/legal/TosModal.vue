<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useMessage } from 'naive-ui'

// viewOnly=false（默认）：强制同意流程；不响应 ESC/遮罩点击，只能勾选+同意。
// viewOnly=true：随时查看用，无 checkbox/同意按钮；ESC、遮罩点击、X 都可关闭。
const props = withDefaults(defineProps<{ viewOnly?: boolean }>(), { viewOnly: false })
const emit = defineEmits<{ close: [] }>()

const authStore = useAuthStore()
const message = useMessage()

const agreed = ref(false)
const submitting = ref(false)

const onSubmit = async () => {
  if (!agreed.value || submitting.value) return
  submitting.value = true
  try {
    await authStore.acceptTos()
  } catch (err) {
    console.error('[TosModal] 提交同意失败:', err)
    message.error('提交失败，请检查网络后重试')
    submitting.value = false
    return
  }
  // 成功后由 needsTos 计算属性变 false，组件自然卸载，不需要手动关闭
}

const onOverlayClick = (e: MouseEvent) => {
  if (!props.viewOnly) return
  if (e.target === e.currentTarget) emit('close')
}

const onEscape = (e: KeyboardEvent) => {
  if (props.viewOnly && e.key === 'Escape') emit('close')
}

onMounted(() => {
  if (props.viewOnly) document.addEventListener('keydown', onEscape)
})
onBeforeUnmount(() => {
  document.removeEventListener('keydown', onEscape)
})
</script>

<template>
  <!-- 强制模式：不响应 ESC/遮罩；查看模式：ESC/遮罩/X 均可关闭 -->
  <div
    class="tos-overlay"
    role="dialog"
    aria-modal="true"
    aria-labelledby="tos-title"
    @click="onOverlayClick"
  >
    <div class="tos-card">
      <div class="tos-header">
        <span id="tos-title" class="tos-title">东方炒炒币 — 用户须知</span>
        <button
          v-if="viewOnly"
          type="button"
          class="tos-close-btn"
          aria-label="关闭"
          @click="emit('close')"
        >×</button>
      </div>

      <div class="tos-body">
        <p class="tos-intro">
          本网站为基于东方 Project 的<strong>非官方、非营利同人社团项目</strong>，
          与 ZUN 及任何商业实体无关联。继续使用即视为您已阅读并同意以下条款：
        </p>

        <ol class="tos-clauses">
          <li>
            <strong>性质</strong>
            本系统为同好趣味活动与 LMSR 预测市场机制学习演示，不是真实金融交易平台。
            所有「买入 / 卖出 / 盈亏 / 利息 / 清算」等表述均为游戏内数值或教学类比。
          </li>
          <li>
            <strong>不涉及真实财物</strong>
            本系统不接受任何形式的真实金钱充值、押金、对价，不支付任何现金、返利、
            利息、分红、奖酬。系统中的盈亏、排行榜不代表现实损益。
          </li>
          <li>
            <strong>不构成金融服务</strong>
            本系统不提供证券、期货、基金、外汇、贷款、支付、虚拟货币交易或任何需许可的
            金融服务，不构成投资建议或收益承诺。
          </li>
          <li>
            <strong>不构成博彩活动</strong>
            本系统不组织或便利任何真实金钱投注，不设可兑换现金 / 财物 / 积分权益的游戏
            结果。用户在系统中的任何数值均无现实价值。
          </li>
          <li>
            <strong>使用边界</strong>
            用户不得将本系统用于现实金钱结算、账号交易、奖品兑换、商业推广、融资或任何
            违反法律的用途。任何人将本系统中的价格、模型或策略示例套用于现实金融或高风险
            行为，后果自负。
          </li>
          <li>
            <strong>未成年用户</strong>
            未成年人应在监护人知情同意下参与。如所在地区对模拟交易 / 同人活动有特殊限制，
            请自行遵循当地法规。
          </li>
        </ol>

        <p class="tos-foot-note">
          完整版本见<a class="tos-link" href="/docs/rules" target="_blank" rel="noopener">规则手册</a>。
        </p>
      </div>

      <!-- 强制模式：checkbox + 同意；查看模式：仅一个关闭按钮 -->
      <div v-if="!viewOnly" class="tos-footer">
        <label class="tos-checkbox-row">
          <input
            type="checkbox"
            v-model="agreed"
            class="tos-checkbox"
            :disabled="submitting"
          />
          <span class="tos-checkbox-text">我已阅读并同意以上条款</span>
        </label>

        <button
          class="tos-submit"
          :disabled="!agreed || submitting"
          @click="onSubmit"
          :title="!agreed ? '请先勾选同意' : ''"
        >
          {{ submitting ? '提交中…' : '同意并继续' }}
        </button>
      </div>
      <div v-else class="tos-footer tos-footer-view">
        <button class="tos-submit" @click="emit('close')">关闭</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tos-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  z-index: 9999;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px 16px;
  /* 不响应遮罩点击：父层无 click 处理，子层冒泡也不会触发关闭 */
}

.tos-card {
  position: relative;
  width: 100%;
  max-width: 640px;
  max-height: calc(100vh - 48px);
  display: flex;
  flex-direction: column;
  border: 4px solid #000;
  background: #fff;
  box-shadow: 8px 8px 0 #000;
}

/* 内层细线装饰（与 hero / NotFound 一致） */
.tos-card::before {
  content: '';
  position: absolute;
  inset: 6px;
  border: 1px solid #000;
  pointer-events: none;
}

.tos-header {
  flex: 0 0 auto;
  padding: 14px 24px;
  background: #000;
  border-bottom: 2px solid #000;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.tos-title {
  font-size: 14px;
  font-weight: 700;
  color: #fff;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.tos-close-btn {
  appearance: none;
  background: transparent;
  border: 1px solid #fff;
  color: #fff;
  width: 24px;
  height: 24px;
  font-size: 16px;
  font-weight: 700;
  line-height: 1;
  cursor: pointer;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.tos-close-btn:hover {
  background: #fff;
  color: #000;
}

.tos-footer-view {
  align-items: flex-end;
}

.tos-body {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 20px 24px;
  font-size: 13px;
  line-height: 1.7;
  color: #222;
}

.tos-intro {
  margin-bottom: 14px;
}

.tos-intro strong {
  font-weight: 700;
}

.tos-clauses {
  list-style: none;
  padding: 0;
  margin: 0 0 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  counter-reset: tos-counter;
}

.tos-clauses li {
  position: relative;
  padding-left: 28px;
  counter-increment: tos-counter;
}

.tos-clauses li::before {
  content: counter(tos-counter) ".";
  position: absolute;
  left: 0;
  top: 0;
  font-weight: 700;
  color: #000;
  font-variant-numeric: tabular-nums;
}

.tos-clauses strong {
  display: block;
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #000;
  margin-bottom: 2px;
}

.tos-foot-note {
  font-size: 12px;
  color: #666;
  margin-top: 8px;
}

.tos-link {
  color: #000;
  text-decoration: underline;
  text-underline-offset: 3px;
  font-weight: 600;
}

.tos-link:hover {
  color: #333;
}

.tos-footer {
  flex: 0 0 auto;
  padding: 14px 24px;
  border-top: 2px solid #000;
  background: #f5f5f5;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.tos-checkbox-row {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
}

.tos-checkbox {
  width: 16px;
  height: 16px;
  margin: 0;
  accent-color: #000;
  cursor: pointer;
}

.tos-checkbox-text {
  font-size: 13px;
  font-weight: 600;
  color: #000;
}

.tos-submit {
  align-self: flex-end;
  padding: 10px 28px;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.04em;
  background: #000;
  color: #fff;
  border: 2px solid #000;
  box-shadow: 4px 4px 0 #444;
  cursor: pointer;
  transition: transform 0.1s, box-shadow 0.1s, background 0.15s;
}

.tos-submit:hover:not(:disabled) {
  transform: translate(-1px, -1px);
  box-shadow: 5px 5px 0 #444;
}

.tos-submit:disabled {
  background: #ccc;
  border-color: #ccc;
  color: #888;
  box-shadow: 4px 4px 0 #ddd;
  cursor: not-allowed;
}

@media (max-width: 640px) {
  .tos-card {
    box-shadow: 4px 4px 0 #000;
    max-height: calc(100vh - 24px);
  }
  .tos-header { padding: 12px 16px; }
  .tos-body { padding: 16px; font-size: 12.5px; }
  .tos-footer { padding: 12px 16px; }
  .tos-submit {
    align-self: stretch;
    box-shadow: 3px 3px 0 #444;
  }
  .tos-submit:hover:not(:disabled) {
    box-shadow: 4px 4px 0 #444;
  }
}
</style>
