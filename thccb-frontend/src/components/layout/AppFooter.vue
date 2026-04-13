<script setup lang="ts">
const currentYear = new Date().getFullYear()
</script>

<template>
  <div class="app-footer">
    <span class="footer-copy">© {{ currentYear }} 东方炒炒币</span>
    <span class="footer-sep">|</span>
    <span class="footer-tagline">预测市场交易平台</span>
    <div class="footer-links">
      <span class="footer-text">Powered by LMSR</span>
    </div>
  </div>
</template>

<style scoped>
.app-footer {
  height: 100%;
  padding: 0 20px;
  display: flex;
  align-items: center;
  gap: 12px;
  background: #000000;
  color: #888888;
  font-size: 12px;
}

.footer-copy {
  color: #cccccc;
  font-weight: 500;
}

.footer-sep {
  color: #444444;
}

.footer-tagline {
  flex: 1;
}

.footer-links {
  display: flex;
  gap: 16px;
}

.footer-text {
  color: #666666;
}
</style>
