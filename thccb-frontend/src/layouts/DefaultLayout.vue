<script setup lang="ts">
import { ref, computed, watch } from 'vue'
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

// 移动端侧边栏展开状态
const isMobile = ref(window.innerWidth <= 768)
if (typeof window !== 'undefined') {
  window.addEventListener('resize', () => { isMobile.value = window.innerWidth <= 768 })
}

// 移动端路由切换时自动收起侧边栏
watch(() => route.path, () => {
  if (isMobile.value) collapsed.value = true
})

// 页面标题
const pageTitle = computed(() => {
  return route.meta?.title || '东方炒炒币'
})

// 多级面包屑：从 route.matched 提取每一级的 title
const breadcrumbs = computed(() => {
  return route.matched
    .filter(r => r.meta?.title && r.name !== 'home')
    .map(r => ({
      title: r.meta.title as string,
      path: r.path
    }))
})

// 是否显示侧边栏（通过路由名判断，避免运行时正则）
const hideSidebarNames = new Set(['trading-view'])
const showSidebar = computed(() => {
  return !route.matched.some(r => hideSidebarNames.has(r.name as string))
})
</script>

<template>
  <NLayout class="min-h-screen">
    <!-- 顶部导航栏 -->
    <NLayoutHeader position="absolute" class="h-16" style="border-bottom: 2px solid #000000; z-index: 100;">
      <AppHeader
        :collapsed="collapsed"
        @toggle-collapse="collapsed = !collapsed"
      />
    </NLayoutHeader>

    <NLayout has-sider position="absolute" class="top-16 bottom-12">
      <!-- 移动端遮罩层 -->
      <div
        v-if="isMobile && showSidebar && authStore.isAuthenticated && !collapsed"
        class="sidebar-overlay"
        @click="collapsed = true"
      ></div>

      <!-- 侧边栏 -->
      <NLayoutSider
        v-if="showSidebar && authStore.isAuthenticated"
        :collapsed="collapsed"
        :collapsed-width="64"
        :width="240"
        collapse-mode="width"
        @collapse="collapsed = true"
        @expand="collapsed = false"
        style="border-right: 2px solid #000000;"
      >
        <AppSidebar :collapsed="collapsed" />
      </NLayoutSider>

      <!-- 主要内容区域 -->
      <NLayoutContent :native-scrollbar="false" class="p-4 md:p-6">
        <div class="mx-auto w-full max-w-[1320px]">
          <!-- 多级面包屑导航（移动端隐藏，节省纵向空间） -->
          <div v-if="route.meta?.breadcrumb !== false" class="breadcrumb-bar mb-4" style="border-bottom: 1px solid #e0e0e0; padding-bottom: 10px;">
            <n-breadcrumb>
              <n-breadcrumb-item>
                <router-link to="/" class="text-black">首页</router-link>
              </n-breadcrumb-item>
              <n-breadcrumb-item v-for="crumb in breadcrumbs" :key="crumb.path">
                {{ crumb.title }}
              </n-breadcrumb-item>
            </n-breadcrumb>
          </div>

          <!-- 页面内容 -->
          <slot>
            <router-view />
          </slot>
        </div>
      </NLayoutContent>
    </NLayout>

    <!-- 底部 -->
    <NLayoutFooter position="absolute" class="bottom-0 h-12" style="border-top: 2px solid #000000;">
      <AppFooter />
    </NLayoutFooter>
  </NLayout>
</template>

<style scoped>
.min-h-screen {
  min-height: 100vh;
}

/* 移动端遮罩层 */
.sidebar-overlay {
  position: fixed;
  inset: 0;
  top: 64px;
  z-index: 999;
  background: rgba(0, 0, 0, 0.4);
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

/* 移动端隐藏面包屑 */
@media (max-width: 640px) {
  .breadcrumb-bar {
    display: none;
  }
}
</style>