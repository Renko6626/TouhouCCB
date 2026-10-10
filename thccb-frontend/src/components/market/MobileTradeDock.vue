<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import type { TradeSide } from '@/composables/useMobileTradeEntry'

withDefaults(
  defineProps<{
    visible: boolean
    label?: string
    side: TradeSide
    disabled?: boolean
    showShort?: boolean
    shortActive?: boolean
    showSell?: boolean
    buyLabel?: string
    sellLabel?: string
    shortLabel?: string
  }>(),
  {
    label: '交易',
    disabled: false,
    showShort: false,
    shortActive: false,
    showSell: true,
    buyLabel: '买入',
    sellLabel: '卖出',
    shortLabel: '做空',
  },
)

const emit = defineEmits<{ select: [side: TradeSide]; short: [] }>()
const editing = ref(false)
const updateEditing = () => {
  const active = document.activeElement
  editing.value =
    active instanceof HTMLElement &&
    (active.matches('input, textarea, select') || active.isContentEditable)
}
const onFocusOut = () => {
  queueMicrotask(updateEditing)
}
onMounted(() => {
  updateEditing()
  document.addEventListener('focusin', updateEditing)
  document.addEventListener('focusout', onFocusOut)
})
onBeforeUnmount(() => {
  document.removeEventListener('focusin', updateEditing)
  document.removeEventListener('focusout', onFocusOut)
})
</script>

<template>
  <div v-if="visible && !editing" class="mobile-trade-dock" role="group" aria-label="快捷交易入口">
    <span class="dock-label" :title="label">{{ label }}</span>
    <button
      type="button"
      class="dock-button dock-buy"
      :disabled="disabled"
      :aria-pressed="!shortActive && side === 'buy'"
      :aria-label="`${buyLabel}${label}，前往交易面板`"
      @click="emit('select', 'buy')"
    >
      {{ buyLabel }} <span aria-hidden="true">↓</span>
    </button>
    <button
      v-if="showSell"
      type="button"
      class="dock-button dock-sell"
      :disabled="disabled"
      :aria-pressed="!shortActive && side === 'sell'"
      :aria-label="`${sellLabel}${label}，前往交易面板`"
      @click="emit('select', 'sell')"
    >
      {{ sellLabel }} <span aria-hidden="true">↓</span>
    </button>
    <button
      v-if="showShort"
      type="button"
      class="dock-button dock-short"
      :disabled="disabled"
      :aria-pressed="shortActive"
      :aria-label="`${shortLabel}${label}，前往交易面板`"
      @click="emit('short')"
    >
      {{ shortLabel }} <span aria-hidden="true">↓</span>
    </button>
  </div>
</template>

<style scoped>
.mobile-trade-dock {
  position: fixed;
  inset: auto 0 0;
  z-index: 90;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px max(16px, env(safe-area-inset-right, 0px))
    calc(12px + env(safe-area-inset-bottom, 0px)) max(16px, env(safe-area-inset-left, 0px));
  border-top: 2px solid #000;
  background: #fff;
  box-shadow: 0 -3px 0 #eeeeee;
}
.dock-label {
  flex: 1 1 0;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 700;
}
.dock-button {
  flex: 1 0 72px;
  min-height: 44px;
  border: 2px solid #000;
  padding: 9px 12px;
  background: #000;
  color: #fff;
  font: inherit;
  font-size: 14px;
  font-weight: 800;
  cursor: pointer;
}
.dock-short {
  background: #f0f0f0;
  color: #000;
}
@media (max-width: 400px) {
  .mobile-trade-dock {
    gap: 6px;
  }
  .dock-button {
    padding: 9px 6px;
    flex-basis: 64px;
  }
}
.dock-sell {
  background: #fff;
  color: #000;
}
.dock-button:focus-visible {
  outline: 3px solid #555;
  outline-offset: 3px;
}
.dock-button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
@media (min-width: 1280px) {
  .mobile-trade-dock {
    display: none;
  }
}
</style>
