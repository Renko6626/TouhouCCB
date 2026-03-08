<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { NLayout, NLayoutHeader, NLayoutContent, NLayoutFooter, NLayoutSider } from 'naive-ui'
import { useAuthStore } from '@/stores/auth'
import AppHeader from '@/components/layout/AppHeader.vue'
import AppSidebar from '@/components/layout/AppSidebar.vue'
import AppFooter from '@/components/layout/AppFooter.vue'

const route = useRoute()
const authStore = useAuthStore()

// 侧边栏折叠状态
const collapsed = ref(false)

// 页面标题
const pageTitle = computed(() => {
  return route.meta?.title || '东方炒炒币'
})

// 是否显示侧边栏
const showSidebar = computed(() => {
  // 根据路由决定是否显示侧边栏
  const hideSidebarRoutes = ['/market/:id/trade']
  return !hideSidebarRoutes.some(pattern => 
    new RegExp(pattern.replace(/:\w+/g, '\\w+')).test(route.path)
  )
})
</script>

<template>
  <NLayout class="min-h-screen">
    <!-- 顶部导航栏 -->
    <NLayoutHeader bordered class="h-16">
      <AppHeader 
        :collapsed="collapsed" 
        @toggle-collapse="collapsed = !collapsed" 
      />
    </NLayoutHeader>

    <NLayout has-sider position="absolute" class="top-16 bottom-16">
      <!-- 侧边栏 -->
      <NLayoutSider
        v-if="showSidebar && authStore.isAuthenticated"
        :collapsed="collapsed"
        :collapsed-width="64"
        :width="240"
        bordered
        collapse-mode="width"
        show-trigger
        @collapse="collapsed = true"
        @expand="collapsed = false"
        class="transition-all duration-300"
      >
        <AppSidebar :collapsed="collapsed" />
      </NLayoutSider>

      <!-- 主要内容区域 -->
      <NLayoutContent
        :native-scrollbar="false"
        :class="{ 'ml-64': showSidebar && !collapsed && authStore.isAuthenticated, 'ml-16': showSidebar && collapsed && authStore.isAuthenticated }"
        class="transition-all duration-300 p-6"
      >
        <!-- 面包屑导航 -->
        <div v-if="route.meta?.breadcrumb !== false" class="mb-6">
          <n-breadcrumb>
            <n-breadcrumb-item>
              <router-link to="/">首页</router-link>
            </n-breadcrumb-item>
            <n-breadcrumb-item v-if="route.meta?.title">
              {{ pageTitle }}
            </n-breadcrumb-item>
          </n-breadcrumb>
        </div>

        <!-- 页面标题 -->
        <div v-if="route.meta?.showTitle !== false" class="mb-6">
          <h1 class="text-2xl font-bold text-gray-800 dark:text-gray-200">
            {{ pageTitle }}
          </h1>
          <p v-if="route.meta?.description" class="text-gray-600 dark:text-gray-400 mt-2">
            {{ route.meta.description }}
          </p>
        </div>

        <!-- 页面内容 -->
        <div class="bg-white dark:bg-gray-800 rounded-lg shadow-sm p-6">
          <slot>
            <router-view />
          </slot>
        </div>
      </NLayoutContent>
    </NLayout>

    <!-- 底部 -->
    <NLayoutFooter bordered position="absolute" class="h-16 bottom-0">
      <AppFooter />
    </NLayoutFooter>
  </NLayout>
</template>

<style scoped>
.min-h-screen {
  min-height: 100vh;
}

/* 响应式调整 */
@media (max-width: 768px) {
  .n-layout-sider {
    position: fixed !important;
    z-index: 1000;
    height: calc(100vh - 64px);
  }
  
  .n-layout-content {
    margin-left: 0 !important;
  }
}
</style>