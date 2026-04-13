<script setup lang="ts">
import { ref, onErrorCaptured } from 'vue'
import { RouterView } from 'vue-router'
import { NConfigProvider, NNotificationProvider, NDialogProvider, NLoadingBarProvider, NMessageProvider } from 'naive-ui'
import type { GlobalThemeOverrides } from 'naive-ui'
import { useNotificationStore } from '@/stores/notification'

// 使用通知store
const notificationStore = useNotificationStore()

// 全局错误边界：捕获子组件运行时错误，避免白屏
const globalError = ref<string | null>(null)
onErrorCaptured((err) => {
  console.error('[App] 组件运行时错误:', err)
  globalError.value = err instanceof Error ? err.message : '页面渲染出错'
  return false // 阻止错误继续向上传播
})

// 工业风高对比度黑白主题配置
const industrialThemeOverrides = {
  common: {
    primaryColor: '#000000',
    primaryColorHover: '#111111',
    primaryColorPressed: '#222222',
    primaryColorSuppl: '#000000',
    textColorBase: '#000000',
    textColor1: '#000000',
    textColor2: '#333333',
    textColor3: '#666666',
    textColorDisabled: '#8a8a8a',
    placeholderColor: '#666666',
    placeholderColorDisabled: '#9a9a9a',
    iconColor: '#000000',
    iconColorHover: '#000000',
    iconColorPressed: '#000000',
    borderColor: '#000000',
    borderRadius: '0px',
  },
  Button: {
    color: '#f2f2f2',
    colorHover: '#ebebeb',
    colorPressed: '#e3e3e3',
    colorFocus: '#ebebeb',
    colorDisabled: '#f7f7f7',
    textColor: '#000000',
    textColorHover: '#000000',
    textColorPressed: '#000000',
    textColorFocus: '#000000',
    textColorDisabled: '#777777',
    border: '2px solid #000000',
    borderHover: '2px solid #000000',
    borderPressed: '2px solid #000000',
    borderFocus: '2px solid #000000',
    borderDisabled: '2px solid #000000',
  },
  Card: {
    boxShadow: '6px 6px 0 #000000',
    boxShadowHover: '6px 6px 0 #000000',
    border: '2px solid #000000',
  },
  Input: {
    color: '#ffffff',
    colorHover: '#ffffff',
    colorPressed: '#ffffff',
    colorFocus: '#ffffff',
    textColor: '#000000',
    textColorDisabled: '#777777',
    border: '2px solid #000000',
    borderHover: '2px solid #000000',
    borderPressed: '2px solid #000000',
    borderFocus: '2px solid #000000',
  }
} as GlobalThemeOverrides
</script>

<template>
  <NConfigProvider :theme-overrides="industrialThemeOverrides">
    <NLoadingBarProvider>
      <NDialogProvider>
        <NMessageProvider>
          <NNotificationProvider>
            <!-- 全局通知组件 -->
            <div class="fixed top-4 right-4 z-50 w-80 space-y-2">
              <div
                v-for="notification in notificationStore.notifications"
                :key="notification.id"
                class="p-4 border-2 border-black bg-white text-black shadow-[4px_4px_0_0_#000]"
              >
                <div class="flex justify-between items-start">
                  <div class="flex-1">
                    <div class="font-medium">{{ notification.title }}</div>
                    <div class="text-sm mt-1">{{ notification.message }}</div>
                  </div>
                  <button
                    @click="notificationStore.removeNotification(notification.id)"
                    class="ml-2 border border-black px-2 leading-none hover:bg-black hover:text-white"
                  >
                    ×
                  </button>
                </div>
              </div>
            </div>

            <!-- 全局错误边界 -->
            <div v-if="globalError" class="fixed inset-0 z-50 flex items-center justify-center bg-white">
              <div class="text-center p-12 border-4 border-black" style="box-shadow: 8px 8px 0 #000; max-width: 400px;">
                <div class="text-5xl font-black mb-4">ERR</div>
                <p class="text-sm mb-6" style="color:#444;">{{ globalError }}</p>
                <button
                  class="px-6 py-3 bg-black text-white font-bold border-2 border-black cursor-pointer"
                  style="box-shadow: 4px 4px 0 #444;"
                  @click="globalError = null"
                >
                  重新加载
                </button>
              </div>
            </div>

            <!-- 主应用内容 -->
            <RouterView v-if="!globalError" v-slot="{ Component }">
              <Transition name="fade" mode="out-in">
                <component :is="Component" />
              </Transition>
            </RouterView>
          </NNotificationProvider>
        </NMessageProvider>
      </NDialogProvider>
    </NLoadingBarProvider>
  </NConfigProvider>
</template>

<style>
/* 全局样式 - 工业风高对比度黑白风格 */
body {
  margin: 0;
  padding: 0;
  font-family: 'IBM Plex Sans', 'Segoe UI', 'Noto Sans SC', -apple-system, BlinkMacSystemFont, sans-serif;
  background-color: #ffffff;
  color: #000000;
}

/* 滚动条样式 */
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

::-webkit-scrollbar-track {
  background: #efefef;
}

::-webkit-scrollbar-thumb {
  background: #000000;
}

::-webkit-scrollbar-thumb:hover {
  background: #111111;
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
  line-clamp: 1;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.line-clamp-2 {
  display: -webkit-box;
  line-clamp: 2;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.line-clamp-3 {
  display: -webkit-box;
  line-clamp: 3;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
