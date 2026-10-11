<script setup lang="ts">
import { formatFxAmount, multiplyFxAmount } from '@/api/fx'
import { fxMarginPercent } from '@/utils/fxPresentation'

defineProps<{
  equity: string | null | undefined
  basis: string | null | undefined
  initial: string | number | null | undefined
  caution: boolean
}>()
</script>

<template>
  <div class="post-margin" :class="{ caution }">
    <span>成交后保证金率<small>全账户 / 开仓门槛</small></span>
    <strong>
      {{ fxMarginPercent(equity, basis) }} / ≥{{
        initial == null ? '—' : `${formatFxAmount(multiplyFxAmount(String(initial), '100'), 2)}%`
      }}
    </strong>
  </div>
</template>

<style scoped>
.post-margin {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 6px;
  border-top: 1px solid #ddd;
  font-size: 12px;
}
span {
  color: #777;
}
small {
  display: block;
  font-size: 10px;
  line-height: 1.5;
}
strong {
  text-align: right;
  min-width: 0;
  overflow-wrap: anywhere;
}
.caution strong {
  color: var(--color-down);
}
</style>
