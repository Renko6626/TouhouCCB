<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { NSwitch } from 'naive-ui'
import { authIdentityGuard } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { useTradingSessionStore } from '@/stores/tradingSession'

const auth = useAuthStore(), session = useTradingSessionStore()
const error = ref('')
onMounted(() => { void auth.fetchCurrentUser() })
watch(() => auth.sessionGeneration, () => { error.value = '' }, { flush: 'sync' })
async function save(enabled: boolean) {
  const valid = authIdentityGuard()
  error.value = ''
  try {
    if (await auth.setAutoRepay(enabled) && valid()) session.requestRefresh(undefined, 'auto_repay')
  } catch {
    if (valid()) error.value = '自动还债设置保存失败，请重试。'
  }
}
</script>
<template>
  <div class="auto-repay-switch">
    <div class="auto-repay-control">
      <span>自动还债</span>
      <NSwitch :value="auth.autoRepay" :disabled="auth.autoRepaySaving || !auth.isAuthenticated" :loading="auth.autoRepaySaving" size="small" aria-label="自动还债" @update:value="save" />
    </div>
    <p v-if="error" class="auto-repay-error" role="alert">{{ error }}</p>
  </div>
</template>
<style scoped>
.auto-repay-control { display: flex; align-items: center; gap: 10px; }
.auto-repay-error { margin: 4px 0 0; color: var(--color-down, #b52819); font-size: 11px; }
</style>
