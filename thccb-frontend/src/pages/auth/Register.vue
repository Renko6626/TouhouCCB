<script setup lang="ts">
import { onMounted } from 'vue'
import { getRegisterUrl } from '@/api/casdoor'

onMounted(() => {
  window.location.href = getRegisterUrl()
})
</script>

<template>
  <div class="redirect-hint">
    <p>正在跳转到注册页...</p>
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
