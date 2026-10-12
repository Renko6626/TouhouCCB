<script setup lang="ts">
import { computed } from 'vue'
import { compareFxAmounts, divideFxAmount } from '@/api/fx'
import { fxOrderScale } from '@/utils/fxPresentation'

const props = defineProps<{
  modelValue: string
  principal: string | null
  limit: number | null
  disabled: boolean
  label: string
  tone: 'long' | 'short'
}>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
// More space for small orders. Only slider coordinates use floating point;
// the emitted monetary amount always uses the existing decimal helpers.
const knots = computed(() => {
  const limit = props.limit ?? 1
  return [...new Set([0, 0.25, 0.5, 1, 2, 3, 5, 10, 20, 50].filter((v) => v < limit).concat(limit))]
})
const multiplier = computed(() =>
  /^\d+(?:\.\d{0,6})?$/.test(props.modelValue.trim())
    ? divideFxAmount(props.modelValue, props.principal, 6)
    : null,
)
// The amount is truncated to six decimals. Round only the displayed ratio so
// choosing 0.25× cannot appear as 0.24× after that monetary truncation.
const displayedMultiplier = computed(() =>
  multiplier.value == null ? null : Number(multiplier.value).toFixed(2),
)
const canSelect = computed(
  () => !props.disabled && compareFxAmounts(props.principal, '0') === 1 && props.limit != null,
)
const sliderPosition = computed(() => {
  const value = Number(multiplier.value ?? 0)
  const values = knots.value
  for (let i = 1; i < values.length; i++) {
    if (value <= values[i]!)
      return Math.round(
        ((i - 1 + (value - values[i - 1]!) / (values[i]! - values[i - 1]!)) / (values.length - 1)) *
          1000,
      )
  }
  return 1000
})
const presets = computed(() =>
  [...new Set([0.25, 0.5, 1, 2, props.limit ?? 1])].filter((v) => v <= (props.limit ?? 1)),
)
function choose(value: number) {
  if (!canSelect.value) return
  const amount = fxOrderScale(props.principal, value.toFixed(2))
  if (amount != null) emit('update:modelValue', amount)
}
function onSlide(event: Event) {
  const position =
    (Number((event.target as HTMLInputElement).value) / 1000) * (knots.value.length - 1)
  const index = Math.min(Math.floor(position), knots.value.length - 2)
  choose(knots.value[index]! + (knots.value[index + 1]! - knots.value[index]!) * (position - index))
}
function onInput(event: Event) {
  if (!props.disabled) emit('update:modelValue', (event.target as HTMLInputElement).value)
}
</script>

<template>
  <div class="order-size" :class="tone">
    <div class="size-heading">
      <div class="multiplier">
        <span>本次规模</span><strong>{{ displayedMultiplier ?? '—' }}<small> 倍本金</small></strong>
      </div>
    </div>
    <input
      class="size-range"
      type="range"
      min="0"
      max="1000"
      step="1"
      :value="sliderPosition"
      :disabled="!canSelect"
      aria-label="本次开仓规模相对本金的倍数"
      :aria-valuetext="
        displayedMultiplier == null ? '本金或规模待更新' : `${displayedMultiplier} 倍本金`
      "
      :style="{ '--progress': `${sliderPosition / 10}%` }"
      @input="onSlide"
    />
    <div class="scale" aria-hidden="true">
      <span v-for="value in knots" :key="value"
        >{{ value }}{{ value === knots[knots.length - 1] ? '倍' : '' }}</span
      >
    </div>
    <div class="presets" role="group" aria-label="快捷开仓倍数">
      <button
        v-for="value in presets"
        :key="value"
        type="button"
        :disabled="!canSelect"
        :class="{ selected: compareFxAmounts(displayedMultiplier, String(value)) === 0 }"
        :aria-pressed="compareFxAmounts(displayedMultiplier, String(value)) === 0"
        @click="choose(value)"
      >
        {{ value }} 倍
      </button>
    </div>
    <label class="amount"
      ><span>{{ label }} <small>金圆券</small></span>
      <input
        :value="modelValue"
        inputmode="decimal"
        autocomplete="off"
        placeholder="输入金额"
        :disabled="disabled"
        @input="onInput"
      />
    </label>
    <p v-if="principal == null">暂不能按倍数选规模，请输入金额。</p>
    <p v-if="limit != null && Number(multiplier) > limit">已超出滑条范围。</p>
  </div>
</template>

<style scoped>
.order-size {
  --size-color: #000;
  display: grid;
  gap: 6px;
  margin-bottom: var(--trade-panel-gap);
}
.order-size.short {
  --size-color: var(--color-down);
}
.size-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.principal,
.multiplier {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 12px;
}
.principal > span {
  color: #666;
}
small {
  font-size: 11px;
  font-weight: 400;
  color: #777;
}
.multiplier > span {
  font-size: 11px;
  color: #777;
}
.multiplier > strong {
  color: var(--size-color);
  font-size: 18px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.multiplier small {
  color: inherit;
  font-size: 11px;
}
.size-range {
  appearance: none;
  width: 100%;
  height: 6px;
  margin: 5px 0 2px;
  border-radius: 0;
  background: linear-gradient(to right, var(--size-color) var(--progress), #e5e5e5 var(--progress));
  cursor: pointer;
}
.size-range::-webkit-slider-thumb {
  appearance: none;
  width: 20px;
  height: 20px;
  border: 3px solid var(--size-color);
  border-radius: 50%;
  background: #fff;
}
.size-range::-moz-range-thumb {
  width: 15px;
  height: 15px;
  border: 3px solid var(--size-color);
  border-radius: 50%;
  background: #fff;
}
.scale {
  display: flex;
  justify-content: space-between;
  color: #777;
  font-size: 10px;
}
.presets {
  display: flex;
  gap: 4px;
}
.presets button {
  flex: 1;
  min-width: 0;
  padding: 4px 0;
  min-height: var(--trade-control-height);
  background: #fff;
  border: 2px solid #000;
  color: #000;
  font-size: 11px;
  cursor: pointer;
}
.presets button.selected {
  background: #000;
  color: #fff;
  font-weight: 700;
}
.amount {
  display: grid;
  gap: 5px;
  font-size: 12px;
  font-weight: 650;
}
.amount > span {
  display: flex;
  justify-content: space-between;
}
.amount input {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  min-height: var(--trade-control-height);
  padding: 5px 8px;
  border: 2px solid #000;
  background: #fff;
  color: #000;
  font: inherit;
  font-size: 14px;
  font-variant-numeric: tabular-nums;
}
p {
  margin: 0;
  color: #777;
  font-size: 11px;
  line-height: 1.6;
}
input:disabled,
button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
:is(input, button):focus-visible {
  outline: 2px solid var(--size-color);
  outline-offset: 4px;
}
@media (max-width: 640px) {
  .amount input {
    min-height: 44px;
    font-size: 16px;
  }
  .presets button {
    min-height: 38px;
  }
  .size-range {
    margin-top: 12px;
    margin-bottom: 8px;
  }
}
</style>
