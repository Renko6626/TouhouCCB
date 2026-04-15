<script setup lang="ts">
import { onMounted } from 'vue'
import { getLoginUrl } from '@/api/casdoor'

// 直接跳转到 Casdoor 登录页，不渲染本地页面
onMounted(() => {
  window.location.href = getLoginUrl()
})
</script>

<template>
  <div class="redirect-hint">
    <p>正在跳转到登录页...</p>
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
</style>
