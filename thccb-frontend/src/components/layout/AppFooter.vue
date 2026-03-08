<script setup lang="ts">
const currentYear = new Date().getFullYear()
</script>

<template>
  <div class="app-footer">
    <div class="text-center text-sm text-gray-600 dark:text-gray-400">
      <p>&copy; {{ currentYear }} 东方炒炒币 - 预测市场交易平台</p>
      <p class="mt-1">
        <span class="cursor-pointer hover:text-primary-600 dark:hover:text-primary-400">关于我们</span>
        <span class="mx-2">|</span>
        <span class="cursor-pointer hover:text-primary-600 dark:hover:text-primary-400">使用条款</span>
        <span class="mx-2">|</span>
        <span class="cursor-pointer hover:text-primary-600 dark:hover:text-primary-400">隐私政策</span>
        <span class="mx-2">|</span>
        <span class="cursor-pointer hover:text-primary-600 dark:hover:text-primary-400">联系我们</span>
      </p>
    </div>
  </div>
</template>

<style scoped>
.app-footer {
  padding: 24px 0;
  border-top: 1px solid #e5e7eb;
}

.dark .app-footer {
  border-top-color: #374151;
}
</style>