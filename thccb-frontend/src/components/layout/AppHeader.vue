<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { NButton, NAvatar, NDropdown, NSpace } from 'naive-ui'
import type { DropdownOption } from 'naive-ui'

interface Props {
  collapsed: boolean
}

const props = defineProps<Props>()
const emit = defineEmits<{
  toggleCollapse: []
}>()

const router = useRouter()
const authStore = useAuthStore()

// 用户下拉菜单选项
const userOptions: DropdownOption[] = [
  {
    label: '我的资产',
    key: 'portfolio',
    icon: () => '📊'
  },
  {
    label: '退出登录',
    key: 'logout',
    icon: () => '🚪'
  }
]

// 处理用户菜单点击
const handleUserMenuClick = (key: string) => {
  switch (key) {
    case 'portfolio':
      router.push('/user/portfolio')
      break
    case 'logout':
      authStore.logout()
      router.push('/auth/login')
      break
  }
}

// 切换侧边栏
const toggleSidebar = () => {
  emit('toggleCollapse')
}

// 返回首页
const goHome = () => {
  router.push('/')
}

// 去市场列表
const goMarkets = () => {
  router.push('/market/list')
}
</script>

<template>
  <div class="app-header h-full px-6 flex items-center justify-between">
    <!-- 左侧：Logo和菜单切换 -->
    <div class="flex items-center gap-4">
      <div v-if="authStore.isAuthenticated" 
           @click="toggleSidebar" 
           class="cursor-pointer text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100">
        <i :class="props.collapsed ? 'i-mdi-menu' : 'i-mdi-menu-open'" class="text-xl"></i>
      </div>
      <div @click="goHome" class="cursor-pointer flex items-center gap-2">
        <div class="w-8 h-8 bg-primary-500 rounded-full flex items-center justify-center">
          <span class="text-white font-bold text-sm">T</span>
        </div>
        <span class="text-lg font-semibold text-gray-800 dark:text-gray-200">
          东方炒炒币
        </span>
      </div>
    </div>

    <!-- 右侧：导航和用户信息 -->
    <div class="flex items-center gap-4">
      <!-- 未登录状态 -->
      <div v-if="!authStore.isAuthenticated" class="flex items-center gap-2">
        <NButton text @click="router.push('/auth/login')">
          登录
        </NButton>
        <NButton type="primary" @click="router.push('/auth/register')">
          注册
        </NButton>
      </div>

      <!-- 已登录状态 -->
      <div v-else class="flex items-center gap-4">
        <NSpace>
          <NButton text @click="goMarkets">
            市场列表
          </NButton>
          <NButton text @click="router.push('/user/portfolio')">
            我的资产
          </NButton>
        </NSpace>

        <NDropdown :options="userOptions" @select="handleUserMenuClick">
          <div class="cursor-pointer flex items-center gap-2 hover:bg-gray-100 dark:hover:bg-gray-800 p-2 rounded-lg">
            <NAvatar round size="small">
              {{ authStore.user?.username?.charAt(0).toUpperCase() || 'U' }}
            </NAvatar>
            <span class="text-sm text-gray-700 dark:text-gray-300">
              {{ authStore.user?.username || '用户' }}
            </span>
          </div>
        </NDropdown>
      </div>
    </div>
  </div>
</template>

<style scoped>
.app-header {
  display: flex;
  align-items: center;
}
</style>