<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { NSpin } from 'naive-ui'

const router = useRouter()
const authStore = useAuthStore()

const error = ref('')

/** 校验重定向路径，防止开放重定向攻击 */
const sanitizeRedirect = (raw: string | null): string => {
  if (!raw) return '/'
  // 必须以单个 / 开头，且不能是 // (会被浏览器解析为协议相对URL)
  if (!raw.startsWith('/') || raw.startsWith('//')) return '/'
  // 排除含协议头的变体 (如 /\evil.com)
  if (/^\/[\\]/.test(raw)) return '/'
  return raw
}

onMounted(async () => {
  const params = new URLSearchParams(window.location.search)
  const code = params.get('code')
  const state = params.get('state') ?? ''

  if (!code) {
    error.value = '缺少授权码，请重新登录。'
    return
  }

  // 校验 OAuth2 state 参数，防止 CSRF
  const savedState = sessionStorage.getItem('oauth_state')
  sessionStorage.removeItem('oauth_state')
  if (savedState && state !== savedState) {
    error.value = '安全校验失败（state 不匹配），请重新登录。'
    return
  }

  try {
    await authStore.loginWithCallback(code, state)
    const redirect = sanitizeRedirect(params.get('redirect'))
    router.replace(redirect)
  } catch (err: any) {
    error.value = err?.data?.detail || err?.message || '登录失败，请重试。'
  }
})
</script>

<template>
  <div class="callback-page">
    <div v-if="!error" class="callback-loading">
      <NSpin size="large" />
      <p class="callback-text">正在完成登录...</p>
    </div>

    <div v-else class="callback-error">
      <div class="error-code">ERR</div>
      <p class="error-msg">{{ error }}</p>
      <button class="retry-btn" @click="router.push('/auth/login')">返回登录</button>
    </div>
  </div>
</template>

<style scoped>
.callback-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #ffffff;
}

.callback-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
}

.callback-text {
  font-size: 13px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #888888;
}

.callback-error {
  text-align: center;
  padding: 48px 56px;
  border: 4px solid #000000;
  box-shadow: 8px 8px 0 #000000;
}

.error-code {
  font-size: 48px;
  font-weight: 900;
  color: #000000;
  letter-spacing: -0.02em;
  margin-bottom: 12px;
}

.error-msg {
  font-size: 13px;
  color: #444444;
  margin-bottom: 24px;
  max-width: 280px;
}

.retry-btn {
  padding: 10px 24px;
  font-size: 13px;
  font-weight: 700;
  background: #000000;
  color: #ffffff;
  border: 2px solid #000000;
  box-shadow: 4px 4px 0 #444444;
  cursor: pointer;
}

.retry-btn:hover {
  transform: translate(-1px, -1px);
  box-shadow: 5px 5px 0 #444444;
}
</style>
