<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useUserStore } from '@/stores/user'
import { NDropdown } from 'naive-ui'
import type { DropdownOption } from 'naive-ui'
import TitleChip from '@/components/title/TitleChip.vue'
import { buildMismatch } from '@/composables/useBuildVersion'
const refreshPage = () => location.reload()

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
const equippedTitle = computed(() => userStore.summary?.equipped_title ?? null)

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
    case 'admin': router.push('/admin/markets'); break
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
            <TitleChip v-if="equippedTitle" :title="equippedTitle" size="sm" />
            <i class="i-mdi-chevron-down text-xs"></i>
          </div>
        </NDropdown>
      </template>
    </div>
  </div>

  <!-- build 版本自刷横幅（阶段 2）：检查只在建立 SSE（进交易页/断线重连/切后台回来）时发生，
       停留在 Portfolio 等无 SSE 页面的旧 tab 拿不到提示——阶段 3 的 NaN 兜底主要保护交易页，可接受。 -->
  <Teleport to="body">
    <div v-if="buildMismatch" class="build-refresh-bar">
      <span>站点已更新——当前页面运行的是旧版本，继续操作可能出错</span>
      <button type="button" class="build-refresh-btn" @click="refreshPage">立即刷新</button>
    </div>
  </Teleport>
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
  border: 2px solid var(--color-down);
  color: var(--color-down);
  background: #000;
  font-size: 12px;
  font-weight: 700;
  text-decoration: none;
  letter-spacing: 0.02em;
  transition: background 0.15s, color 0.15s;
}
.debt-badge:hover {
  background: var(--color-down);
  color: #fff;
}

@media (max-width: 480px) {
  .app-header { padding: 0 12px; }
  .brand-name { display: none; }
  .user-name { display: none; }
  .debt-badge { font-size: 11px; padding: 2px 6px; }
}

/* build 版本自刷横幅 */
.build-refresh-bar {
  position: fixed;
  top: 0; left: 0; right: 0;
  z-index: 9999;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 16px;
  padding: 10px 16px;
  background: #000;
  color: #fff;
  border-bottom: 3px solid #f5a623;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.04em;
}
.build-refresh-btn {
  padding: 4px 14px;
  border: 2px solid #fff;
  background: #fff;
  color: #000;
  font-size: 12px;
  font-weight: 800;
  cursor: pointer;
}
.build-refresh-btn:hover { background: #f5a623; border-color: #f5a623; }
</style>
