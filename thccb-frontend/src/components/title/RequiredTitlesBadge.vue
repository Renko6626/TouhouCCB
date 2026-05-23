<script setup lang="ts">
// 市场卡片 / 列表场景下的"准入门槛"小标识。
// titles 非空时显示锁图标 + 称号 chip；用户未达到 (canTrade=false) 再加一行红字提示。
import type { TitleChip as TitleChipType } from '@/api/title'
import TitleChip from './TitleChip.vue'

defineProps<{
  titles: TitleChipType[]
  canTrade: boolean
}>()
</script>

<template>
  <div v-if="titles.length" class="required-titles-badge">
    <span class="lock-icon" aria-hidden="true">🔒</span>
    <span class="prefix-label">需要</span>
    <TitleChip v-for="t in titles" :key="t.id" :title="t" size="sm" />
    <span v-if="!canTrade" class="reject-hint">（你未达到）</span>
  </div>
</template>

<style scoped>
.required-titles-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}

.lock-icon {
  font-size: 11px;
  line-height: 1;
}

.prefix-label {
  font-size: 11px;
  font-weight: 600;
  color: #444;
  letter-spacing: 0.04em;
}

.reject-hint {
  font-size: 11px;
  font-weight: 700;
  color: #cc0000;
  letter-spacing: 0.04em;
}
</style>
