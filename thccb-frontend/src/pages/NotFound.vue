<script setup lang="ts">
import { useRouter } from 'vue-router'
const router = useRouter()
</script>

<template>
  <div class="not-found">
    <div class="nf-card">
      <div class="nf-code">404</div>
      <div class="nf-divider"></div>
      <p class="nf-message">页面未找到</p>
      <button class="nf-btn" @click="router.push('/')">返回首页</button>
    </div>
  </div>
</template>

<style scoped>
.not-found {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #ffffff;
}

.nf-card {
  text-align: center;
  padding: 56px 64px;
  border: 4px solid #000000;
  box-shadow: 8px 8px 0 #000000;
  background: #ffffff;
  position: relative;
}

/* 内层细线装饰 */
.nf-card::before {
  content: '';
  position: absolute;
  inset: 6px;
  border: 1px solid #000000;
  pointer-events: none;
}

.nf-code {
  font-size: 96px;
  font-weight: 900;
  color: #000000;
  line-height: 1;
  letter-spacing: -0.04em;
  font-variant-numeric: tabular-nums;
}

.nf-divider {
  width: 48px;
  height: 3px;
  background: #000000;
  margin: 20px auto;
}

.nf-message {
  font-size: 13px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: #888888;
  margin-bottom: 32px;
}

.nf-btn {
  padding: 10px 28px;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.03em;
  background: #000000;
  color: #ffffff;
  border: 2px solid #000000;
  box-shadow: 4px 4px 0 #444444;
  cursor: pointer;
  transition: transform 0.1s, box-shadow 0.1s;
}

.nf-btn:hover {
  transform: translate(-1px, -1px);
  box-shadow: 5px 5px 0 #444444;
}
</style>
