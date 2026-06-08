<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getLoginUrl } from '@/api/casdoor'
import { useAuthStore } from '@/stores/auth'

// 仅本地开发（vite dev / build --mode development）展示 mock 登录入口。
// 生产 bundle 中 import.meta.env.DEV 为 false，保持原行为：直接跳 Casdoor。
const isDev = import.meta.env.DEV
const auth = useAuthStore()
const router = useRouter()

const devUsername = ref('dev')
const devLoading = ref(false)
const devError = ref('')

onMounted(async () => {
  if (!isDev) {
    window.location.href = await getLoginUrl()
  }
})

async function gotoCasdoor() {
  window.location.href = await getLoginUrl()
}

async function doDevLogin() {
  devError.value = ''
  devLoading.value = true
  try {
    await auth.loginWithDev(devUsername.value.trim() || 'dev')
    router.push('/')
  } catch (e) {
    devError.value = e instanceof Error ? e.message : 'dev 登录失败'
  } finally {
    devLoading.value = false
  }
}
</script>

<template>
  <div v-if="!isDev" class="redirect-hint">
    <p>正在跳转到登录页...</p>
  </div>

  <div v-else class="dev-login">
    <div class="dev-box">
      <h2>DEV 登录</h2>
      <p class="hint">本地开发用 mock 登录（无需 Casdoor）。生产环境此入口不存在。</p>
      <label>
        用户名
        <input
          v-model="devUsername"
          type="text"
          placeholder="dev"
          @keyup.enter="doDevLogin"
        />
      </label>
      <button :disabled="devLoading" @click="doDevLogin">
        {{ devLoading ? '登录中...' : 'Dev 登录' }}
      </button>
      <p v-if="devError" class="err">{{ devError }}</p>
      <button class="link" @click="gotoCasdoor">改用 Casdoor 登录 →</button>
    </div>
  </div>
</template>

<style scoped>
.redirect-hint {
  min-height: 60vh;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  color: #888;
}

.dev-login {
  min-height: 60vh;
  display: flex;
  align-items: center;
  justify-content: center;
}

.dev-box {
  width: 320px;
  border: 2px solid #111;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.dev-box h2 {
  margin: 0;
  font-size: 18px;
  letter-spacing: 1px;
}

.dev-box .hint {
  margin: 0;
  font-size: 12px;
  color: #888;
}

.dev-box label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: #444;
}

.dev-box input {
  border: 2px solid #111;
  padding: 8px;
  font-size: 14px;
}

.dev-box button {
  border: 2px solid #111;
  background: #111;
  color: #fff;
  padding: 8px;
  cursor: pointer;
  font-size: 14px;
}

.dev-box button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.dev-box button.link {
  background: none;
  color: #444;
  border: none;
  font-size: 12px;
  padding: 0;
  text-align: left;
}

.dev-box .err {
  margin: 0;
  font-size: 12px;
  color: #c00;
}
</style>
