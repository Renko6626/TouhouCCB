<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { siteAccessApi } from '@/api/siteAccess'
import { useAuthStore } from '@/stores/auth'
import TakeoverScene from '@/components/access/TakeoverScene.vue'
import InertSiteShell from '@/components/access/InertSiteShell.vue'

const router = useRouter()
const auth = useAuthStore()
const checking = ref(false)
const error = ref('')

async function checkAccess() {
  if (checking.value) return
  checking.value = true
  error.value = ''
  try {
    const { disabled } = await siteAccessApi.status()
    const user = disabled && auth.accessToken ? await auth.fetchCurrentUser() : null
    if (!disabled || user?.is_superuser) await router.replace('/')
  } catch {
    error.value = '暂时无法检查访问状态，请稍后重试。'
  } finally {
    checking.value = false
  }
}
onMounted(checkAccess)
</script>

<template>
  <TakeoverScene>
    <template #source><InertSiteShell /></template>
    <template #actions>
      <p class="closure-copy">交易系统暂时下线</p>
      <div class="closure-actions">
        <button :disabled="checking" @click="checkAccess">
          {{ checking ? '正在检查…' : '重新检查' }}
        </button>
        <button @click="router.push({ name: 'login', query: { admin: '1' } })">管理员登录</button>
      </div>
      <p v-if="error" role="status" class="closure-error">{{ error }}</p>
    </template>
  </TakeoverScene>
</template>

<style scoped>
.closure-copy {
  margin: 0 0 18px;
  font-size: clamp(12px, 1vw, 18px);
  line-height: 1.7;
}
.closure-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}
button {
  border: 1px solid currentColor;
  background: #000;
  color: inherit;
  font: inherit;
  font-size: 13px;
  padding: 10px 18px;
  cursor: pointer;
}
button:hover {
  background: #ff1738;
  color: #000;
  border-color: #ff1738;
}
button:disabled {
  opacity: 0.5;
  cursor: wait;
}
button:focus-visible {
  outline: 2px solid currentColor;
  outline-offset: 4px;
}
.closure-error {
  margin-top: 12px;
  max-width: 30em;
  font-size: 12px;
}
</style>
