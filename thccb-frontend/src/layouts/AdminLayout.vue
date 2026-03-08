<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { NLayout, NLayoutHeader, NLayoutContent, NLayoutSider, NMenu } from 'naive-ui'
import type { MenuOption } from 'naive-ui'
import { useAuthStore } from '@/stores/auth'
import AppHeader from '@/components/layout/AppHeader.vue'

const route = useRoute()
const authStore = useAuthStore()

// 侧边栏折叠状态
const collapsed = ref(false)

// 页面标题
const pageTitle = computed(() => {
  return route.meta?.title || '管理员面板'
})

// 管理员菜单选项
const adminMenuOptions: MenuOption[] = [
  {
    label: '市场管理',
    key: 'market-manage',
    icon: () => h('i', { class: 'i-mdi-chart-line' }),
    children: [
      {
        label: '创建市场',
        key: 'create-market',
        icon: () => h('i', { class: 'i-mdi-plus-circle' })
      },
      {
        label: '市场列表',
        key: 'market-list',
        icon: () => h('i', { class: 'i-mdi-format-list-bulleted' })
      }
    ]
  },
  {
    label: '激活码管理',
    key: 'activation-codes',
    icon: () => h('i', { class: 'i-mdi-key-variant' }),
    children: [
      {
        label: '生成激活码',
        key: 'generate-codes',
        icon: () => h('i', { class: 'i-mdi-key-plus' })
      },
      {
        label: '激活码列表',
        key: 'codes-list',
        icon: () => h('i', { class: 'i-mdi-key-chain' })
      }
    ]
  },
  {
    label: '系统监控',
    key: 'system-monitor',
    icon: () => h('i', { class: 'i-mdi-monitor-dashboard' }),
    children: [
      {
        label: '系统状态',
        key: 'system-status',
        icon: () => h('i', { class: 'i-mdi-heart-pulse' })
      },
      {
        label: '用户统计',
        key: 'user-stats',
        icon: () => h('i', { class: 'i-mdi-account-group' })
      },
      {
        label: '交易统计',
        key: 'trade-stats',
        icon: () => h('i', { class: 'i-mdi-chart-bar' })
      }
    ]
  },
  {
    label: '用户管理',
    key: 'user-management',
    icon: () => h('i', { class: 'i-mdi-account-cog' }),
    children: [
      {
        label: '用户列表',
        key: 'user-list',
        icon: () => h('i', { class: 'i-mdi-account-box-multiple' })
      },
      {
        label: '权限管理',
        key: 'permission-management',
        icon: () => h('i', { class: 'i-mdi-shield-account' })
      }
    ]
  },
  {
    label: '系统设置',
    key: 'system-settings',
    icon: () => h('i', { class: 'i-mdi-cog' }),
    children: [
      {
        label: '平台配置',
        key: 'platform-config',
        icon: () => h('i', { class: 'i-mdi-tune' })
      },
      {
        label: '通知设置',
        key: 'notification-settings',
        icon: () => h('i', { class: 'i-mdi-bell-cog' })
      },
      {
        label: '日志查看',
        key: 'system-logs',
        icon: () => h('i', { class: 'i-mdi-text-box-search' })
      }
    ]
  }
]

// 处理菜单选择
const handleMenuSelect = (key: string) => {
  const routeMap: Record<string, string> = {
    'market-manage': '/admin/market-manage',
    'activation-codes': '/admin/activation-codes',
    'system-monitor': '/admin/system-monitor'
  }
  
  if (routeMap[key]) {
    router.push(routeMap[key])
  }
}
</script>

<template>
  <NLayout class="min-h-screen">
    <!-- 顶部导航栏 -->
    <NLayoutHeader bordered class="h-16">
      <AppHeader 
        :collapsed="collapsed" 
        @toggle-collapse="collapsed = !collapsed" 
        show-admin-badge
      />
    </NLayoutHeader>

    <NLayout has-sider position="absolute" class="top-16 bottom-0">
      <!-- 管理员侧边栏 -->
      <NLayoutSider
        :collapsed="collapsed"
        :collapsed-width="64"
        :width="280"
        bordered
        collapse-mode="width"
        show-trigger
        @collapse="collapsed = true"
        @expand="collapsed = false"
        class="transition-all duration-300"
      >
        <!-- 管理员面板标题 -->
        <div class="p-4 border-b border-gray-200 dark:border-gray-700">
          <div v-if="!collapsed" class="flex items-center">
            <div class="w-8 h-8 bg-purple-500 rounded-lg flex items-center justify-center mr-3">
              <i class="i-mdi-shield-account text-white"></i>
            </div>
            <div>
              <h3 class="font-bold text-gray-800 dark:text-gray-200">管理员面板</h3>
              <p class="text-xs text-gray-500 dark:text-gray-400">系统管理控制台</p>
            </div>
          </div>
          <div v-else class="flex justify-center">
            <div class="w-8 h-8 bg-purple-500 rounded-lg flex items-center justify-center">
              <i class="i-mdi-shield-account text-white"></i>
            </div>
          </div>
        </div>

        <!-- 管理员菜单 -->
        <NMenu
          :collapsed="collapsed"
          :collapsed-width="64"
          :collapsed-icon-size="22"
          :options="adminMenuOptions"
          :value="route.name as string"
          @update:value="handleMenuSelect"
          class="mt-4"
        />
      </NLayoutSider>

      <!-- 主要内容区域 -->
      <NLayoutContent
        :native-scrollbar="false"
        :class="{ 'ml-280': !collapsed, 'ml-64': collapsed }"
        class="transition-all duration-300 p-6"
      >
        <!-- 管理员操作栏 -->
        <div class="mb-6 flex justify-between items-center">
          <div>
            <h1 class="text-2xl font-bold text-gray-800 dark:text-gray-200">
              {{ pageTitle }}
            </h1>
            <p v-if="route.meta?.description" class="text-gray-600 dark:text-gray-400 mt-2">
              {{ route.meta.description }}
            </p>
          </div>
          
          <!-- 快速操作按钮 -->
          <div class="flex space-x-2">
            <n-button type="primary" size="small" v-if="route.name === 'market-manage'">
              <template #icon>
                <i class="i-mdi-plus"></i>
              </template>
              创建市场
            </n-button>
            <n-button type="info" size="small" v-if="route.name === 'activation-codes'">
              <template #icon>
                <i class="i-mdi-key-plus"></i>
              </template>
              生成激活码
            </n-button>
            <n-button type="warning" size="small" v-if="route.name === 'system-monitor'">
              <template #icon>
                <i class="i-mdi-refresh"></i>
              </template>
              刷新数据
            </n-button>
          </div>
        </div>

        <!-- 统计卡片 -->
        <div v-if="route.name === 'system-monitor'" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
          <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
            <div class="flex items-center">
              <div class="w-12 h-12 bg-blue-100 dark:bg-blue-900 rounded-lg flex items-center justify-center mr-4">
                <i class="i-mdi-account-group text-blue-600 dark:text-blue-300 text-2xl"></i>
              </div>
              <div>
                <p class="text-sm text-gray-500 dark:text-gray-400">总用户数</p>
                <p class="text-2xl font-bold text-gray-800 dark:text-gray-200">1,234</p>
              </div>
            </div>
          </div>
          
          <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
            <div class="flex items-center">
              <div class="w-12 h-12 bg-green-100 dark:bg-green-900 rounded-lg flex items-center justify-center mr-4">
                <i class="i-mdi-chart-line text-green-600 dark:text-green-300 text-2xl"></i>
              </div>
              <div>
                <p class="text-sm text-gray-500 dark:text-gray-400">活跃市场</p>
                <p class="text-2xl font-bold text-gray-800 dark:text-gray-200">42</p>
              </div>
            </div>
          </div>
          
          <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
            <div class="flex items-center">
              <div class="w-12 h-12 bg-purple-100 dark:bg-purple-900 rounded-lg flex items-center justify-center mr-4">
                <i class="i-mdi-cash-multiple text-purple-600 dark:text-purple-300 text-2xl"></i>
              </div>
              <div>
                <p class="text-sm text-gray-500 dark:text-gray-400">今日交易额</p>
                <p class="text-2xl font-bold text-gray-800 dark:text-gray-200">¥ 56,789</p>
              </div>
            </div>
          </div>
          
          <div class="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
            <div class="flex items-center">
              <div class="w-12 h-12 bg-orange-100 dark:bg-orange-900 rounded-lg flex items-center justify-center mr-4">
                <i class="i-mdi-server text-orange-600 dark:text-orange-300 text-2xl"></i>
              </div>
              <div>
                <p class="text-sm text-gray-500 dark:text-gray-400">系统状态</p>
                <p class="text-2xl font-bold text-green-600 dark:text-green-400">正常</p>
              </div>
            </div>
          </div>
        </div>

        <!-- 页面内容 -->
        <div class="bg-white dark:bg-gray-800 rounded-lg shadow-sm p-6">
          <slot>
            <router-view />
          </slot>
        </div>
      </NLayoutContent>
    </NLayout>
  </NLayout>
</template>

<style scoped>
.min-h-screen {
  min-height: 100vh;
}

.ml-280 {
  margin-left: 280px;
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
  
  .ml-280, .ml-64 {
    margin-left: 0 !important;
  }
}
</style>