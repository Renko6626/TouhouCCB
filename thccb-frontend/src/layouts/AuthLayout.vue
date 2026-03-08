<script setup lang="ts">
import { NLayout, NLayoutContent, NLayoutFooter } from 'naive-ui'
import { useRoute } from 'vue-router'

const route = useRoute()

// 页面标题
const pageTitle = route.meta?.title || '东方炒炒币'
</script>

<template>
  <NLayout class="min-h-screen bg-gradient-to-br from-primary-50 to-gray-50 dark:from-gray-900 dark:to-gray-800">
    <!-- 主要内容区域 -->
    <NLayoutContent class="flex items-center justify-center p-4">
      <div class="w-full max-w-md">
        <!-- Logo和标题 -->
        <div class="text-center mb-8">
          <div class="flex justify-center mb-4">
            <div class="w-16 h-16 bg-primary-500 rounded-full flex items-center justify-center">
              <span class="text-white text-2xl font-bold">T</span>
            </div>
          </div>
          <h1 class="text-3xl font-bold text-gray-800 dark:text-gray-200 mb-2">
            东方炒炒币
          </h1>
          <p class="text-gray-600 dark:text-gray-400">
            预测市场交易平台
          </p>
        </div>

        <!-- 认证卡片 -->
        <div class="bg-white dark:bg-gray-800 rounded-xl shadow-lg p-8">
          <!-- 页面标题 -->
          <div class="text-center mb-6">
            <h2 class="text-2xl font-bold text-gray-800 dark:text-gray-200">
              {{ pageTitle }}
            </h2>
            <p v-if="route.meta?.description" class="text-gray-600 dark:text-gray-400 mt-2">
              {{ route.meta.description }}
            </p>
          </div>

          <!-- 页面内容 -->
          <slot>
            <router-view />
          </slot>

          <!-- 底部链接 -->
          <div class="mt-8 pt-6 border-t border-gray-200 dark:border-gray-700">
            <div class="flex justify-between text-sm">
              <router-link 
                v-if="route.name !== 'login'" 
                to="/auth/login" 
                class="text-primary-600 hover:text-primary-700 dark:text-primary-400 dark:hover:text-primary-300"
              >
                已有账号？立即登录
              </router-link>
              <router-link 
                v-if="route.name !== 'register'" 
                to="/auth/register" 
                class="text-primary-600 hover:text-primary-700 dark:text-primary-400 dark:hover:text-primary-300"
              >
                没有账号？立即注册
              </router-link>
              <router-link 
                v-if="route.name === 'login'" 
                to="/" 
                class="text-gray-600 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-300"
              >
                返回首页
              </router-link>
            </div>
          </div>
        </div>
      </div>
    </NLayoutContent>

    <!-- 底部 -->
    <NLayoutFooter class="py-4 text-center text-gray-500 dark:text-gray-400 text-sm">
      <div class="container mx-auto px-4">
        <p>© 2026 东方炒炒币预测市场交易平台. 保留所有权利.</p>
        <p class="mt-1">
          <router-link to="/about" class="hover:text-primary-600 dark:hover:text-primary-400">
            关于我们
          </router-link>
          <span class="mx-2">•</span>
          <a href="#" class="hover:text-primary-600 dark:hover:text-primary-400">
            服务条款
          </a>
          <span class="mx-2">•</span>
          <a href="#" class="hover:text-primary-600 dark:hover:text-primary-400">
            隐私政策
          </a>
        </p>
      </div>
    </NLayoutFooter>
  </NLayout>
</template>

<style scoped>
.min-h-screen {
  min-height: 100vh;
}

/* 动画效果 */
.bg-gradient-to-br {
  background-size: 400% 400%;
  animation: gradient 15s ease infinite;
}

@keyframes gradient {
  0% {
    background-position: 0% 50%;
  }
  50% {
    background-position: 100% 50%;
  }
  100% {
    background-position: 0% 50%;
  }
}

/* 响应式调整 */
@media (max-width: 640px) {
  .max-w-md {
    max-width: 100%;
  }
  
  .p-8 {
    padding: 1.5rem;
  }
}
</style>