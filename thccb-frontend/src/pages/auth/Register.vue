<script setup lang="ts">
import { getRegisterUrl } from '@/api/casdoor'
</script>

<template>
  <div class="sso-entry">
    <p class="sso-hint">账号注册在统一认证平台完成，注册后自动开通游戏账户。</p>
    <button class="sso-btn" @click="() => (window.location.href = getRegisterUrl())">
      前往注册
    </button>
  </div>
</template>

<style scoped>
.sso-entry {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 20px;
  padding: 8px 0;
}

.sso-hint {
  font-size: 13px;
  color: #666666;
  text-align: center;
  line-height: 1.6;
}

.sso-btn {
  width: 100%;
  padding: 12px;
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.04em;
  background: #000000;
  color: #ffffff;
  border: 2px solid #000000;
  box-shadow: 4px 4px 0 #444444;
  cursor: pointer;
  transition: transform 0.1s, box-shadow 0.1s;
}

.sso-btn:hover {
  transform: translate(-1px, -1px);
  box-shadow: 5px 5px 0 #444444;
}
</style>
