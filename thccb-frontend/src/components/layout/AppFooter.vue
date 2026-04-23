<script setup lang="ts">
const currentYear = new Date().getFullYear()
</script>

<template>
  <div class="app-footer">
    <div class="footer-left">
      <span class="footer-copy">© {{ currentYear }} 东方炒炒币</span>
      <span class="footer-sep" aria-hidden="true">/</span>
      <span class="footer-tagline">预测市场交易平台</span>
    </div>
    <div class="footer-right">
      <span class="footer-badge">LMSR</span>
      <span class="footer-text">对数市场评分规则</span>
    </div>
  </div>
</template>

<style scoped>
.app-footer {
  height: 100%;
  padding: 0 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: #ffffff;
  color: #555555;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.02em;
}

.footer-left,
.footer-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.footer-copy {
  color: #000000;
  font-weight: 600;
}

.footer-sep {
  color: #bbbbbb;
}

.footer-tagline {
  color: #666666;
}

.footer-badge {
  display: inline-block;
  padding: 1px 7px;
  background: #000000;
  color: #ffffff;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.08em;
  border: 1.5px solid #000000;
}

.footer-text {
  color: #777777;
}

@media (max-width: 640px) {
  .app-footer { padding: 0 12px; font-size: 10px; }
  .footer-sep,
  .footer-tagline,
  .footer-right .footer-text { display: none; }
}
</style>
