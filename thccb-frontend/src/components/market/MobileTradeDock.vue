<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import type { TradeSide } from '@/composables/useMobileTradeEntry'

withDefaults(defineProps<{
  visible: boolean
  label?: string
  side: TradeSide
  disabled?: boolean
}>(), { label: '交易', disabled: false })

const emit = defineEmits<{ select: [side: TradeSide] }>()
const editing = ref(false)
const updateEditing = () => {
  const active = document.activeElement
  editing.value = active instanceof HTMLElement && (
    active.matches('input, textarea, select') || active.isContentEditable
  )
}
const onFocusOut = () => { queueMicrotask(updateEditing) }
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
      :aria-pressed="side === 'buy'"
      :aria-label="`买入${label}，前往交易面板`"
      @click="emit('select', 'buy')"
    >买入 <span aria-hidden="true">↓</span></button>
    <button
      type="button"
      class="dock-button dock-sell"
      :disabled="disabled"
      :aria-pressed="side === 'sell'"
      :aria-label="`卖出${label}，前往交易面板`"
      @click="emit('select', 'sell')"
    >卖出 <span aria-hidden="true">↓</span></button>
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
  padding: 12px max(16px, env(safe-area-inset-right, 0px)) calc(12px + env(safe-area-inset-bottom, 0px)) max(16px, env(safe-area-inset-left, 0px));
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
.dock-sell { background: #fff; color: #000; }
.dock-button:focus-visible { outline: 3px solid #555; outline-offset: 3px; }
.dock-button:disabled { opacity: 0.45; cursor: not-allowed; }
@media (min-width: 1280px) { .mobile-trade-dock { display: none; } }
</style>
