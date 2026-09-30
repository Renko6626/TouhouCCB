<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

interface Props {
  collapsed: boolean
}

const props = defineProps<Props>()
const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()

// 主导航分组可折叠；管理区仍保持原有展开方式。
type NavLeaf = { label: string; path: string; icon: string; activeIcon: string }
type NavGroup = { label: string; icon: string; activeIcon: string; children: NavLeaf[] }
type NavEntry = NavLeaf | NavGroup
const isGroup = (e: NavEntry): e is NavGroup => 'children' in e

const navItems: NavEntry[] = [
  { label: '首页', path: '/', icon: 'i-mdi-home-outline', activeIcon: 'i-mdi-home' },
  { label: '外汇交易', path: '/fx', icon: 'i-mdi-lightning-bolt', activeIcon: 'i-mdi-lightning-bolt' },
  { label: '我的资产', path: '/user/portfolio', icon: 'i-mdi-wallet-outline', activeIcon: 'i-mdi-wallet' },
  { label: '交易记录', path: '/user/transactions', icon: 'i-mdi-history', activeIcon: 'i-mdi-history' },
  { label: '预测市场', path: '/market/list', icon: 'i-mdi-chart-areaspline', activeIcon: 'i-mdi-chart-areaspline' },
  { label: '排行榜', path: '/market/leaderboard', icon: 'i-mdi-trophy-outline', activeIcon: 'i-mdi-trophy' },
  { label: '借款', path: '/loan', icon: 'i-mdi-cash-multiple', activeIcon: 'i-mdi-cash-multiple' },
  {
    label: '兑换中心',
    icon: 'i-mdi-gift-outline',
    activeIcon: 'i-mdi-gift',
    children: [
      { label: '激活码兑换', path: '/redemption', icon: 'i-mdi-ticket-outline', activeIcon: 'i-mdi-ticket' },
      { label: '弹幕兑换', path: '/danmuku/exchange', icon: 'i-mdi-message-text-outline', activeIcon: 'i-mdi-message-text' },
      { label: '称号兑换', path: '/redeem-title', icon: 'i-mdi-medal-outline', activeIcon: 'i-mdi-medal' },
      { label: '我的兑换', path: '/my/redemptions', icon: 'i-mdi-ticket-confirmation-outline', activeIcon: 'i-mdi-ticket-confirmation' },
    ],
  },
]

// 管理区同样复用 NavEntry 分组结构；激活态按「最长前缀」判定，
// 否则 /admin/users 会同时点亮 /admin/users/batch。
const adminItems: NavEntry[] = [
  {
    label: '用户与资金', icon: 'i-mdi-account-cash-outline', activeIcon: 'i-mdi-account-cash',
    children: [
      { label: '用户管理', path: '/admin/users', icon: 'i-mdi-account-group-outline', activeIcon: 'i-mdi-account-group' },
      { label: '批量操作', path: '/admin/users/batch', icon: 'i-mdi-cash-multiple', activeIcon: 'i-mdi-cash-multiple' },
    ],
  },
  { label: '市场管理', path: '/admin/markets', icon: 'i-mdi-chart-box-outline', activeIcon: 'i-mdi-chart-box' },
  { label: 'FX 管理', path: '/admin/fx', icon: 'i-mdi-currency-usd', activeIcon: 'i-mdi-currency-usd' },
  { label: 'PvE 机器人', path: '/admin/pve', icon: 'i-mdi-robot-outline', activeIcon: 'i-mdi-robot' },
  {
    label: '风控', icon: 'i-mdi-shield-outline', activeIcon: 'i-mdi-shield',
    children: [
      { label: 'Bot 预警', path: '/admin/bot', icon: 'i-mdi-shield-alert-outline', activeIcon: 'i-mdi-shield-alert' },
      { label: '资产统计 / 强平', path: '/admin/wealth-stats', icon: 'i-mdi-chart-bar', activeIcon: 'i-mdi-chart-bar' },
    ],
  },
  {
    label: '兑换与称号', icon: 'i-mdi-gift-outline', activeIcon: 'i-mdi-gift',
    children: [
      { label: '合作方', path: '/admin/redemption/partners', icon: 'i-mdi-handshake-outline', activeIcon: 'i-mdi-handshake' },
      { label: '兑换批次', path: '/admin/redemption/batches', icon: 'i-mdi-package-variant', activeIcon: 'i-mdi-package-variant' },
      { label: '兑换码核销', path: '/admin/redemption/codes', icon: 'i-mdi-ticket-confirmation-outline', activeIcon: 'i-mdi-ticket-confirmation' },
      { label: '称号目录', path: '/admin/titles', icon: 'i-mdi-medal-outline', activeIcon: 'i-mdi-medal' },
      { label: '称号激活码', path: '/admin/title-codes', icon: 'i-mdi-ticket-account', activeIcon: 'i-mdi-ticket-account' },
    ],
  },
  { label: '站点配置', path: '/admin/site-config', icon: 'i-mdi-tune', activeIcon: 'i-mdi-tune' },
]
const adminPaths = adminItems.flatMap(e => (isGroup(e) ? e.children.map(c => c.path) : [e.path]))

const isActive = (path: string) => route.path === path || route.path.startsWith(path + '/')
const expandedGroups = ref<Record<string, boolean>>({})
watch(() => route.path, () => {
  for (const item of navItems) {
    if (isGroup(item) && item.children.some(child => isActive(child.path))) {
      expandedGroups.value[item.label] = true
    }
  }
}, { immediate: true })
const isAdminActive = (path: string) => {
  if (!isActive(path)) return false
  // 有更长的管理路径也匹配当前路由 → 让位给它
  return !adminPaths.some(p => p !== path && p.startsWith(path + '/') && isActive(p))
}

const navigate = (path: string) => router.push(path)
</script>

<template>
  <div class="sidebar" :class="{ collapsed: props.collapsed }">
    <!-- 主导航 -->
    <nav class="sidebar-nav" aria-label="主导航">
      <template v-for="(item, idx) in navItems" :key="isGroup(item) ? `g-${idx}-${item.label}` : item.path">
        <div v-if="idx === 4" class="nav-section-divider">
          <span v-if="!props.collapsed">探索与服务</span>
        </div>
        <!-- 分组父项：窄侧栏也可通过图标展开子项 -->
        <template v-if="isGroup(item)">
          <button
            class="nav-item nav-group-toggle"
            :aria-expanded="!!expandedGroups[item.label]"
            :aria-controls="`nav-group-${idx}`"
            :aria-label="item.label"
            :title="props.collapsed ? item.label : undefined"
            @click="expandedGroups[item.label] = !expandedGroups[item.label]"
          >
            <i :class="[item.icon, 'nav-icon']"></i>
            <span v-if="!props.collapsed" class="nav-label">{{ item.label }}</span>
            <i v-if="!props.collapsed" :class="expandedGroups[item.label] ? 'i-mdi-chevron-up' : 'i-mdi-chevron-down'" class="nav-group-chevron"></i>
          </button>
          <div v-show="expandedGroups[item.label]" :id="`nav-group-${idx}`">
          <button
            v-for="child in item.children"
            :key="child.path"
            :class="['nav-item nav-item-child', { active: isActive(child.path) }]"
            @click="navigate(child.path)"
            :title="props.collapsed ? `${item.label} · ${child.label}` : undefined"
            :aria-label="child.label"
          >
            <i :class="[isActive(child.path) ? child.activeIcon : child.icon, 'nav-icon']"></i>
            <span v-if="!props.collapsed" class="nav-label">{{ child.label }}</span>
          </button>
          </div>
        </template>
        <!-- 叶子项 -->
        <button
          v-else
          :class="['nav-item', { active: isActive(item.path), 'nav-item-featured': item.path === '/fx' }]"
          @click="navigate(item.path)"
          :title="props.collapsed ? item.label : undefined"
          :aria-label="item.label"
        >
          <i :class="[isActive(item.path) ? item.activeIcon : item.icon, 'nav-icon']"></i>
          <span v-if="!props.collapsed" class="nav-label">{{ item.label }}</span>
          <span v-if="!props.collapsed && item.path === '/fx'" class="nav-brand-badge">FX战士</span>
        </button>
      </template>
    </nav>

    <!-- 管理员导航 -->
    <div v-if="authStore.isAdmin" class="sidebar-admin">
      <div v-if="!props.collapsed" class="admin-label">管理</div>
      <nav class="sidebar-nav">
        <template v-for="(item, idx) in adminItems" :key="isGroup(item) ? `ag-${idx}-${item.label}` : item.path">
          <template v-if="isGroup(item)">
            <div v-if="!props.collapsed" class="nav-group-label">
              <i :class="[item.icon, 'nav-group-icon']"></i>
              <span>{{ item.label }}</span>
            </div>
            <button
              v-for="child in item.children"
              :key="child.path"
              :class="['nav-item nav-item-admin nav-item-child', { active: isAdminActive(child.path) }]"
              @click="navigate(child.path)"
              :title="props.collapsed ? `${item.label} · ${child.label}` : undefined"
              :aria-label="child.label"
            >
              <i :class="[isAdminActive(child.path) ? child.activeIcon : child.icon, 'nav-icon']"></i>
              <span v-if="!props.collapsed" class="nav-label">{{ child.label }}</span>
            </button>
          </template>
          <button
            v-else
            :class="['nav-item nav-item-admin', { active: isAdminActive(item.path) }]"
            @click="navigate(item.path)"
            :title="props.collapsed ? item.label : undefined"
            :aria-label="item.label"
          >
            <i :class="[isAdminActive(item.path) ? item.activeIcon : item.icon, 'nav-icon']"></i>
            <span v-if="!props.collapsed" class="nav-label">{{ item.label }}</span>
          </button>
        </template>
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
  min-height: 44px;
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

.nav-item:focus-visible {
  outline: 2px solid #777;
  outline-offset: -3px;
}

.nav-section-divider {
  margin-top: 12px;
  padding: 12px 16px 6px;
  border-top: 1px solid #e8e8e8;
  color: #777;
  font-size: 11px;
  letter-spacing: 0.06em;
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
.nav-item-featured {
  background: #fff3c4;
  color: #332500;
  border-left-color: #777;
  font-weight: 800;
}
.nav-item-featured:hover {
  background: #ffe79a;
}
.nav-item-featured.active {
  background: #000;
  color: #fff;
}

.nav-brand-badge {
  margin-left: auto;
  font-size: 10px;
  font-weight: 500;
  opacity: 0.7;
}

.nav-label {
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 分组小标题：纯展示，不可交互 */
.nav-group-label {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px 4px;
  font-size: 10px;
  font-weight: 800;
  color: #999;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.nav-group-icon {
  font-size: 12px;
}
.nav-group-toggle { font-weight: 700; }
.nav-group-chevron { margin-left: auto; flex-shrink: 0; }
.nav-group-toggle:focus-visible { outline: 2px solid #000; outline-offset: -2px; }

/* 子项：缩进 + 字号略小，与父项视觉分层 */
.nav-item-child {
  padding-left: 32px;
  font-size: 12.5px;
}

.collapsed .nav-item-child {
  padding-left: 12px;
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
