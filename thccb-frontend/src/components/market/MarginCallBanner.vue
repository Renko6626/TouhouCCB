<script setup lang="ts">
import { computed } from 'vue'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()
const status = computed(() => userStore.summary?.margin_status ?? 'healthy')
const ratio = computed(() => userStore.summary?.margin_ratio)

const visible = computed(() => status.value !== 'healthy')

const message = computed(() => {
  if (status.value === 'danger') {
    return `警告: 即将被强平 — 净值/借款 = ${ratio.value != null ? Number(ratio.value).toFixed(3) : '?'} < 0.2，请立即补仓或卖出持仓`
  }
  if (status.value === 'warning') {
    return `警告: 中重仓警报 — 净值/借款 = ${ratio.value != null ? Number(ratio.value).toFixed(3) : '?'} < 0.5，建议补仓或减仓避免被强平`
  }
  return ''
})
</script>

<template>
  <div v-if="visible" class="margin-call-banner" :class="`status-${status}`">
    <span class="banner-label">{{ status === 'danger' ? 'MARGIN CALL' : 'MARGIN WARNING' }}</span>
    <span class="banner-message">{{ message }}</span>
  </div>
</template>

<style scoped>
.margin-call-banner {
  display: flex;
  align-items: baseline;
  gap: 12px;
  padding: 10px 16px;
  border-width: 2px;
  border-style: solid;
  border-radius: 0;
  margin-bottom: 12px;
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
</style>
