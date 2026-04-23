<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import { NBreadcrumb, NBreadcrumbItem } from 'naive-ui'
import { useAuthStore } from '@/stores/auth'
import AppHeader from '@/components/layout/AppHeader.vue'
import AppSidebar from '@/components/layout/AppSidebar.vue'
import AppFooter from '@/components/layout/AppFooter.vue'

const route = useRoute()
const authStore = useAuthStore()

// 侧栏折叠
const collapsed = ref(false)

// 移动端：默认折叠、路由切换自动收起
const isMobile = ref(typeof window !== 'undefined' ? window.innerWidth <= 768 : false)
if (typeof window !== 'undefined') {
  window.addEventListener('resize', () => { isMobile.value = window.innerWidth <= 768 })
}
if (isMobile.value) collapsed.value = true
watch(() => route.path, () => { if (isMobile.value) collapsed.value = true })

// 面包屑（空则隐藏，避免首页只显示"首页"一个字）
const breadcrumbs = computed(() =>
  route.matched
    .filter(r => r.meta?.title && r.name !== 'home')
    .map(r => ({ title: r.meta.title as string, path: r.path }))
)
const showBreadcrumb = computed(
  () => route.meta?.breadcrumb !== false && breadcrumbs.value.length > 0
)

// 哪些路由不显示侧栏（交易页自己是全屏工作台）
const hideSidebarNames = new Set(['trading-view'])
const sidebarActive = computed(() =>
  !route.matched.some(r => hideSidebarNames.has(r.name as string))
  && authStore.isAuthenticated
)
</script>

<template>
  <div class="app-shell">
    <!-- 顶栏 -->
    <header class="app-header-wrap">
      <AppHeader
        :collapsed="collapsed"
        :show-toggle="sidebarActive"
        @toggle-collapse="collapsed = !collapsed"
      />
    </header>

    <!-- 中段：侧栏 + 主体 -->
    <div class="app-body">
      <!-- 移动端遮罩 -->
      <div
        v-if="isMobile && sidebarActive && !collapsed"
        class="sidebar-overlay"
        @click="collapsed = true"
      ></div>

      <!-- 侧栏 -->
      <aside
        v-if="sidebarActive"
        class="app-sidebar-wrap"
        :class="{ collapsed, 'mobile-open': isMobile && !collapsed }"
      >
        <AppSidebar :collapsed="collapsed" />
      </aside>

      <!-- 主体 -->
      <main class="app-main">
        <div class="app-main-inner">
          <nav v-if="showBreadcrumb" class="breadcrumb-bar">
            <NBreadcrumb>
              <NBreadcrumbItem>
                <router-link to="/" class="breadcrumb-link">首页</router-link>
              </NBreadcrumbItem>
              <NBreadcrumbItem v-for="crumb in breadcrumbs" :key="crumb.path">
                {{ crumb.title }}
              </NBreadcrumbItem>
            </NBreadcrumb>
          </nav>

          <slot>
            <router-view />
          </slot>
        </div>
      </main>
    </div>

    <!-- 页脚 -->
    <footer class="app-footer-wrap">
      <AppFooter />
    </footer>
  </div>
</template>

<style scoped>
.app-shell {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
  background: #ffffff;
}

/* 顶栏：sticky 保留可视 + flex 不收缩 */
.app-header-wrap {
  flex: 0 0 auto;
  height: 56px;
  border-bottom: 2px solid #000000;
  position: sticky;
  top: 0;
  z-index: 100;
  background: #000000;
}

/* 中段：填满剩余高度，内部横向排列 */
.app-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  position: relative;
}

/* 侧栏：外层拥有唯一的 border-right，避免和内部重叠 */
.app-sidebar-wrap {
  flex: 0 0 220px;
  width: 220px;
  background: #ffffff;
  border-right: 2px solid #000000;
  transition: flex-basis 0.2s ease, width 0.2s ease;
}
.app-sidebar-wrap.collapsed {
  flex-basis: 64px;
  width: 64px;
}

/* 主体：自身滚动，header/footer 保持可见 */
.app-main {
  flex: 1 1 auto;
  min-width: 0;
  overflow-y: auto;
  background: #ffffff;
}
.app-main-inner {
  max-width: 1320px;
  margin: 0 auto;
  padding: 20px 24px 32px;
}

/* 面包屑 */
.breadcrumb-bar {
  margin-bottom: 16px;
  padding-bottom: 10px;
  border-bottom: 1px solid #e0e0e0;
}
.breadcrumb-link {
  color: #000000;
  text-decoration: none;
}
.breadcrumb-link:hover {
  text-decoration: underline;
}

/* 页脚：反转浅色，降低视觉重量 */
.app-footer-wrap {
  flex: 0 0 auto;
  height: 40px;
  border-top: 2px solid #000000;
  background: #ffffff;
}

/* 遮罩 */
.sidebar-overlay {
  position: fixed;
  top: 56px;
  left: 0;
  right: 0;
  bottom: 40px;
  z-index: 999;
  background: rgba(0, 0, 0, 0.4);
}

/* 移动端：侧栏变 drawer */
@media (max-width: 768px) {
  .app-sidebar-wrap {
    position: fixed;
    top: 56px;
    left: 0;
    bottom: 40px;
    z-index: 1000;
    transform: translateX(-100%);
    transition: transform 0.2s ease;
  }
  .app-sidebar-wrap.mobile-open {
    transform: translateX(0);
  }
  .app-sidebar-wrap.collapsed {
    transform: translateX(-100%);
  }
  .app-main-inner {
    padding: 16px 14px 24px;
  }
  .breadcrumb-bar {
    display: none;
  }
}
</style>
