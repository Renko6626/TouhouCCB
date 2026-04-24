<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useUserStore } from '@/stores/user'
import { NDropdown } from 'naive-ui'
import type { DropdownOption } from 'naive-ui'

interface Props {
  collapsed: boolean
  showToggle?: boolean
}
const props = withDefaults(defineProps<Props>(), { showToggle: true })
const emit = defineEmits<{ toggleCollapse: [] }>()

const router = useRouter()
const authStore = useAuthStore()
const userStore = useUserStore()

const debtAmount = computed(() => Number(userStore.summary?.debt ?? 0))

const userOptions = computed<DropdownOption[]>(() => {
  const options: DropdownOption[] = [
    { label: '我的资产', key: 'portfolio' },
    { label: '交易记录', key: 'transactions' },
  ]
  if (authStore.isAdmin) {
    options.push({ type: 'divider', key: 'div' } as DropdownOption)
    options.push({ label: '管理后台', key: 'admin' })
  }
  options.push({ type: 'divider', key: 'div2' } as DropdownOption)
  options.push({ label: '退出登录', key: 'logout' })
  return options
})

const handleUserMenuClick = (key: string) => {
  switch (key) {
    case 'portfolio': router.push('/user/portfolio'); break
    case 'transactions': router.push('/user/transactions'); break
    case 'admin': router.push('/admin/market-manage'); break
    case 'logout':
      authStore.logout()
      router.push('/auth/login')
      break
  }
}
</script>

<template>
  <div class="app-header">
    <!-- 左：切换 + Logo -->
    <div class="header-left">
      <button
        v-if="props.showToggle && authStore.isAuthenticated"
        class="sidebar-toggle"
        @click="emit('toggleCollapse')"
        :title="props.collapsed ? '展开侧边栏' : '收起侧边栏'"
      >
        <i :class="props.collapsed ? 'i-mdi-menu' : 'i-mdi-menu-open'" class="text-lg"></i>
      </button>

      <router-link to="/" class="brand">
        <div class="brand-logo">T</div>
        <span class="brand-name">东方炒炒币</span>
      </router-link>
    </div>

    <!-- 右：登录/注册 或 用户 chip。主导航挪到 sidebar 了，不在 header 再出现 -->
    <div class="header-right">
      <template v-if="!authStore.isAuthenticated">
        <button class="nav-btn" @click="router.push('/auth/login')">登录</button>
        <button class="nav-btn nav-btn-primary" @click="router.push('/auth/register')">注册</button>
      </template>

      <template v-else>
        <router-link
          v-if="debtAmount > 0"
          to="/loan"
          class="debt-badge"
          :title="`当前负债 ${userStore.summary?.debt}`"
        >
          负债 {{ userStore.summary?.debt }}
        </router-link>
        <NDropdown :options="userOptions" @select="handleUserMenuClick" placement="bottom-end">
          <div class="user-chip">
            <div class="user-avatar">
              {{ authStore.user?.username?.charAt(0).toUpperCase() || 'U' }}
            </div>
            <span class="user-name">{{ authStore.user?.username || '用户' }}</span>
            <i class="i-mdi-chevron-down text-xs"></i>
          </div>
        </NDropdown>
      </template>
    </div>
  </div>
</template>

<style scoped>
.app-header {
  height: 100%;
  padding: 0 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #000000;
  color: #ffffff;
}

/* 左 */
.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.sidebar-toggle {
  width: 30px;
  height: 30px;
  background: none;
  border: 1px solid rgba(255,255,255,0.3);
  color: #ffffff;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s;
  flex-shrink: 0;
}
.sidebar-toggle:hover {
  background: rgba(255,255,255,0.1);
  border-color: rgba(255,255,255,0.55);
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  text-decoration: none;
}
.brand-logo {
  width: 26px;
  height: 26px;
  background: #ffffff;
  color: #000000;
  font-weight: 900;
  font-size: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.brand-name {
  font-size: 14px;
  font-weight: 700;
  color: #ffffff;
  letter-spacing: 0.02em;
  white-space: nowrap;
}

/* 右 */
.header-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* 未登录按钮 */
.nav-btn {
  padding: 5px 12px;
  font-size: 12px;
  font-weight: 600;
  border: 1.5px solid rgba(255,255,255,0.5);
  background: none;
  color: #ffffff;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
  letter-spacing: 0.02em;
}
.nav-btn:hover {
  border-color: #ffffff;
  background: rgba(255,255,255,0.08);
}
.nav-btn-primary {
  background: #ffffff;
  color: #000000;
  border-color: #ffffff;
}
.nav-btn-primary:hover {
  background: #e8e8e8;
}

/* 用户菜单 */
.user-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1.5px solid rgba(255,255,255,0.3);
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
  background: rgba(255,255,255,0.06);
}
.user-chip:hover {
  background: rgba(255,255,255,0.12);
  border-color: rgba(255,255,255,0.6);
}
.user-avatar {
  width: 22px;
  height: 22px;
  background: #ffffff;
  color: #000000;
  font-size: 11px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.user-name {
  font-size: 12px;
  font-weight: 500;
  color: #ffffff;
  max-width: 90px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 负债徽章 */
.debt-badge {
  display: inline-block;
  padding: 3px 10px;
  border: 2px solid #d14;
  color: #d14;
  background: #000;
  font-size: 12px;
  font-weight: 700;
  text-decoration: none;
  letter-spacing: 0.02em;
  transition: background 0.15s, color 0.15s;
}
.debt-badge:hover {
  background: #d14;
  color: #fff;
}

@media (max-width: 480px) {
  .app-header { padding: 0 12px; }
  .brand-name { display: none; }
  .user-name { display: none; }
  .debt-badge { font-size: 11px; padding: 2px 6px; }
}
</style>
