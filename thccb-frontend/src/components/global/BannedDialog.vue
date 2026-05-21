<script setup lang="ts">
// 全局封号弹窗——监听 axios interceptor 触发的 'user-banned' 事件。
// 后端 detail === 'USER_BANNED' 即弹此窗 + 强制 logout + 阻塞操作。
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const show = ref(false)
const router = useRouter()
const authStore = useAuthStore()

function handleBanned() {
  show.value = true
}

function dismiss() {
  show.value = false
  authStore.logout()
  // 跳回首页（已 logout 状态）
  router.push('/').catch(() => {})
}

onMounted(() => {
  window.addEventListener('user-banned', handleBanned)
})

onBeforeUnmount(() => {
  window.removeEventListener('user-banned', handleBanned)
})
</script>

<template>
  <Teleport to="body">
    <Transition name="banned-fade">
      <div v-if="show" class="banned-overlay" @click.self="dismiss">
        <div class="banned-modal">
          <div class="banned-header">
            <div class="banned-icon">⛔</div>
            <div class="banned-title">账号已被封禁</div>
          </div>

          <div class="banned-body">
            <p class="banned-main">
              你的账号已被管理员封禁，<strong>无法继续操作</strong>。
            </p>
            <p class="banned-sub">
              常见原因：高频脚本 / 多账号刷分 / 异常交易模式 / 其他违规行为。
            </p>
            <p class="banned-action-hint">
              如认为是误封，请通过弹幕 / 站外渠道联系管理员申诉。
            </p>
          </div>

          <div class="banned-footer">
            <button class="banned-btn" @click="dismiss">我知道了，退出登录</button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.banned-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.75);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
  padding: 16px;
}
.banned-modal {
  background: #ffffff;
  border: 4px solid #cc0000;
  max-width: 480px;
  width: 100%;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
  position: relative;
}
.banned-modal::before {
  content: '';
  position: absolute;
  inset: -4px;
  border: 2px solid #000;
  pointer-events: none;
}
.banned-header {
  background: #cc0000;
  color: #fff;
  padding: 16px 20px;
  display: flex;
  align-items: center;
  gap: 12px;
  border-bottom: 4px solid #000;
}
.banned-icon {
  font-size: 36px;
  line-height: 1;
}
.banned-title {
  font-size: 22px;
  font-weight: 900;
  letter-spacing: 0.05em;
}
.banned-body {
  padding: 20px;
}
.banned-main {
  font-size: 16px;
  font-weight: 700;
  color: #000;
  margin: 0 0 12px 0;
  line-height: 1.5;
}
.banned-main strong {
  color: #cc0000;
}
.banned-sub {
  font-size: 13px;
  color: #555;
  margin: 0 0 10px 0;
  line-height: 1.6;
}
.banned-action-hint {
  font-size: 12px;
  color: #888;
  margin: 0;
  padding: 8px 10px;
  background: #f5f5f5;
  border-left: 4px solid #888;
}
.banned-footer {
  padding: 12px 20px 16px;
  text-align: center;
}
.banned-btn {
  width: 100%;
  padding: 12px 24px;
  background: #000;
  color: #fff;
  border: none;
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.05em;
  cursor: pointer;
  text-transform: uppercase;
  transition: background 0.15s;
}
.banned-btn:hover {
  background: #333;
}
.banned-btn:active {
  background: #cc0000;
}
.banned-fade-enter-active,
.banned-fade-leave-active {
  transition: opacity 0.2s;
}
.banned-fade-enter-from,
.banned-fade-leave-to {
  opacity: 0;
}
</style>
