<script setup lang="ts">
import { RouterView } from 'vue-router'
import { NConfigProvider, NNotificationProvider, NDialogProvider, NLoadingBarProvider } from 'naive-ui'
import { useNotificationStore } from '@/stores/notification'

// 使用通知store
const notificationStore = useNotificationStore()
</script>

<template>
  <NConfigProvider>
    <NLoadingBarProvider>
      <NDialogProvider>
        <NNotificationProvider>
          <!-- 全局通知组件 -->
          <div class="fixed top-4 right-4 z-50 w-80 space-y-2">
            <div
              v-for="notification in notificationStore.notifications"
              :key="notification.id"
              class="p-4 rounded-lg shadow-lg border"
              :class="{
                'bg-green-50 border-green-200 text-green-800': notification.type === 'success',
                'bg-red-50 border-red-200 text-red-800': notification.type === 'error',
                'bg-yellow-50 border-yellow-200 text-yellow-800': notification.type === 'warning',
                'bg-blue-50 border-blue-200 text-blue-800': notification.type === 'info'
              }"
            >
              <div class="flex justify-between items-start">
                <div class="flex-1">
                  <div class="font-medium">{{ notification.title }}</div>
                  <div class="text-sm mt-1">{{ notification.message }}</div>
                </div>
                <button
                  @click="notificationStore.removeNotification(notification.id)"
                  class="ml-2 text-gray-500 hover:text-gray-700"
                >
                  ×
                </button>
              </div>
            </div>
          </div>

          <!-- 主应用内容 -->
          <RouterView />
        </NNotificationProvider>
      </NDialogProvider>
    </NLoadingBarProvider>
  </NConfigProvider>
</template>

<style>
/* 全局样式 */
body {
  margin: 0;
  padding: 0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  background-color: #f9fafb;
  color: #374151;
}

.dark body {
  background-color: #111827;
  color: #f9fafb;
}

/* 滚动条样式 */
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

::-webkit-scrollbar-track {
  background: #f1f1f1;
  border-radius: 4px;
}

.dark ::-webkit-scrollbar-track {
  background: #374151;
}

::-webkit-scrollbar-thumb {
  background: #c1c1c1;
  border-radius: 4px;
}

.dark ::-webkit-scrollbar-thumb {
  background: #6b7280;
}

::-webkit-scrollbar-thumb:hover {
  background: #a8a8a8;
}

.dark ::-webkit-scrollbar-thumb:hover {
  background: #9ca3af;
}

/* 过渡动画 */
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

/* 工具类 */
.line-clamp-1 {
  display: -webkit-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.line-clamp-3 {
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
