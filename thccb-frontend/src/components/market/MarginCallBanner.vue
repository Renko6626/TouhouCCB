<script setup lang="ts">
import { computed } from 'vue'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()

const status = computed(() => userStore.summary?.risk_status ?? 'healthy')
const visible = computed(() => !!userStore.summary && status.value !== 'healthy')
const message = computed(() => {
  if (status.value === 'blocked') return userStore.summary?.blocked_reason || '账户估值待恢复，请刷新资产概览。'
  if (status.value === 'danger') return '账户保证金率低于强平线，请还款或减少风险持仓。'
  return '账户保证金率低于新增风险门槛，可尝试还款或回补减仓。'
})
</script>

<template>
  <div v-if="visible" class="margin-call-wrap">
    <div class="margin-call-banner" :class="`status-${status}`">
      <span class="banner-label">{{ status === 'blocked' ? '估值待恢复' : '账户风险提醒' }}</span>
      <span class="banner-message">{{ message }}</span>
    </div>
  </div>
</template>

<style scoped>
.margin-call-wrap {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
}

.margin-call-banner {
  display: flex;
  align-items: baseline;
  gap: 12px;
  padding: 10px 16px;
  border-width: 2px;
  border-style: solid;
  border-radius: 0;
  letter-spacing: 0.03em;
}

.banner-label {
  font-size: 11px;
  font-weight: 900;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  white-space: nowrap;
  flex-shrink: 0;
}

.banner-message {
  font-size: 13px;
  font-weight: 600;
  line-height: 1.4;
}

.status-warning {
  background: #fef3c7;
  color: #92400e;
  border-color: #d97706;
}

.status-danger {
  background: #fee2e2;
  color: #991b1b;
  border-color: #dc2626;
  font-weight: 700;
}

.status-protected {
  background: #dbeafe;
  color: #1e3a8a;
  border-color: #2563eb;
}

.just-liquidated-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  align-self: flex-start;
  padding: 6px 12px;
  border: 2px solid #4b5563;
  background: #f3f4f6;
  color: #1f2937;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.04em;
}

.chip-icon {
  font-size: 14px;
  color: #4b5563;
  line-height: 1;
}

.chip-text {
  font-variant-numeric: tabular-nums;
}
</style>
