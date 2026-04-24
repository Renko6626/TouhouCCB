<script setup lang="ts">
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

interface Props {
  collapsed: boolean
}

const props = defineProps<Props>()
const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()

const navItems = [
  { label: '首页', path: '/', icon: 'i-mdi-home-outline', activeIcon: 'i-mdi-home' },
  { label: '市场列表', path: '/market/list', icon: 'i-mdi-chart-areaspline', activeIcon: 'i-mdi-chart-areaspline' },
  { label: '排行榜', path: '/market/leaderboard', icon: 'i-mdi-trophy-outline', activeIcon: 'i-mdi-trophy' },
  { label: '我的资产', path: '/user/portfolio', icon: 'i-mdi-wallet-outline', activeIcon: 'i-mdi-wallet' },
  { label: '交易记录', path: '/user/transactions', icon: 'i-mdi-history', activeIcon: 'i-mdi-history' },
  { label: '借款', path: '/loan', icon: 'i-mdi-cash-multiple', activeIcon: 'i-mdi-cash-multiple' },
]

const adminItems = [
  { label: '管理后台', path: '/admin/market-manage', icon: 'i-mdi-cog-outline' },
  { label: '站点配置', path: '/admin/site-config', icon: 'i-mdi-tune' },
]

const isActive = (path: string) => route.path === path || route.path.startsWith(path + '/')

const navigate = (path: string) => router.push(path)
</script>

<template>
  <div class="sidebar" :class="{ collapsed: props.collapsed }">
    <!-- 主导航 -->
    <nav class="sidebar-nav">
      <button
        v-for="item in navItems"
        :key="item.path"
        :class="['nav-item', { active: isActive(item.path) }]"
        @click="navigate(item.path)"
        :title="props.collapsed ? item.label : undefined"
      >
        <i :class="[isActive(item.path) ? item.activeIcon : item.icon, 'nav-icon']"></i>
        <span v-if="!props.collapsed" class="nav-label">{{ item.label }}</span>
      </button>
    </nav>

    <!-- 管理员导航 -->
    <div v-if="authStore.isAdmin" class="sidebar-admin">
      <div v-if="!props.collapsed" class="admin-label">管理</div>
      <nav class="sidebar-nav">
        <button
          v-for="item in adminItems"
          :key="item.path"
          :class="['nav-item nav-item-admin', { active: isActive(item.path) }]"
          @click="navigate(item.path)"
          :title="props.collapsed ? item.label : undefined"
        >
          <i :class="[item.icon, 'nav-icon']"></i>
          <span v-if="!props.collapsed" class="nav-label">{{ item.label }}</span>
        </button>
      </nav>
    </div>
  </div>
</template>

<style scoped>
.sidebar {
  height: 100%;
  background: #ffffff;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  /* border-right 由 DefaultLayout 的外层 .app-sidebar-wrap 统一提供，
     避免 2px 边框在侧栏组件和外层 aside 上叠 4px。 */
}

/* 导航区 */
.sidebar-nav {
  display: flex;
  flex-direction: column;
  padding: 8px 0;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  font-size: 13px;
  font-weight: 500;
  color: #555555;
  background: none;
  border: none;
  cursor: pointer;
  text-align: left;
  width: 100%;
  border-left: 3px solid transparent;
  transition: background 0.12s, color 0.12s, border-color 0.12s;
  white-space: nowrap;
}

.nav-item:hover {
  background: #f5f5f5;
  color: #000000;
}

.nav-item.active {
  background: #000000;
  color: #ffffff;
  border-left-color: #000000;
  font-weight: 600;
}

.nav-icon {
  font-size: 16px;
  flex-shrink: 0;
}

.nav-label {
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 管理员区 */
.sidebar-admin {
  border-top: 1px solid #e0e0e0;
  margin-top: auto;
  padding-top: 8px;
}

.admin-label {
  padding: 4px 16px 2px;
  font-size: 10px;
  font-weight: 700;
  color: #999999;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.nav-item-admin {
  font-size: 12px;
}

/* 折叠态 */
.collapsed .nav-item {
  padding: 12px;
  justify-content: center;
}

.collapsed .nav-item.active {
  background: #000000;
}
</style>
