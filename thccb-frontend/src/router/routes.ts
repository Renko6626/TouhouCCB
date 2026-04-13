import type { RouteRecordRaw } from 'vue-router'
import NotFound from '@/pages/NotFound.vue'

/**
 * 简化的路由定义 - 只包含实际存在的核心页面
 */
export const routes: RouteRecordRaw[] = [
  // 公共路由
  {
    path: '/',
    name: 'home',
    component: () => import('@/pages/home/Home.vue'),
    meta: {
      title: '首页',
      requiresAuth: false
    }
  },

  // 认证路由
  {
    path: '/auth',
    name: 'auth',
    redirect: '/auth/login',
    meta: {
      hideIfLoggedIn: true
    },
    children: [
      {
        path: 'login',
        name: 'login',
        component: () => import('@/pages/auth/Login.vue'),
        meta: {
          title: '登录',
          hideIfLoggedIn: true
        }
      },
      {
        path: 'register',
        name: 'register',
        component: () => import('@/pages/auth/Register.vue'),
        meta: {
          title: '注册',
          hideIfLoggedIn: true
        }
      }
    ]
  },

  // Casdoor OAuth2 回调（不挂在 /auth 父路由，避免 hideIfLoggedIn 误拦截）
  {
    path: '/auth/callback',
    name: 'auth-callback',
    component: () => import('@/pages/auth/Callback.vue'),
    meta: { title: '登录中', requiresAuth: false },
  },

  // 用户路由
  {
    path: '/user',
    name: 'user',
    redirect: '/user/portfolio',
    meta: {
      requiresAuth: true,
      requiresVerified: true
    },
    children: [
      {
        path: 'portfolio',
        name: 'portfolio',
        component: () => import('@/pages/user/Portfolio.vue'),
        meta: {
          title: '我的资产',
          requiresAuth: true,
          requiresVerified: true
        }
      },
      {
        path: 'transactions',
        name: 'transactions',
        component: () => import('@/pages/user/Transactions.vue'),
        meta: {
          title: '交易记录',
          requiresAuth: true,
          requiresVerified: true
        }
      }
    ]
  },

  // 市场路由
  {
    path: '/market',
    name: 'market',
    redirect: '/market/list',
    meta: {
      requiresAuth: true,
      requiresVerified: true
    },
    children: [
      {
        path: 'list',
        name: 'market-list',
        component: () => import('@/pages/market/MarketList.vue'),
        meta: {
          title: '市场列表',
          requiresAuth: true,
          requiresVerified: true
        }
      },
      {
        path: ':id/trade',
        name: 'trading-view',
        component: () => import('@/pages/market/TradingView.vue'),
        meta: {
          title: '交易视图',
          requiresAuth: true,
          requiresVerified: true
        }
      },
      {
        path: 'leaderboard',
        name: 'leaderboard',
        component: () => import('@/pages/market/Leaderboard.vue'),
        meta: {
          title: '财富排行榜',
          requiresAuth: true,
          requiresVerified: true
        }
      }
    ]
  },

  // 管理员路由（新增）
  {
    path: '/admin',
    name: 'admin',
    redirect: '/admin/market-manage',
    meta: {
      requiresAuth: true,
      requiresAdmin: true
    },
    children: [
      {
        path: 'market-manage',
        name: 'market-manage',
        component: () => import('@/pages/admin/MarketManage.vue'),
        meta: {
          title: '市场管理',
          requiresAuth: true,
          requiresAdmin: true
        }
      },
      {
        path: 'system-monitor',
        name: 'system-monitor',
        component: () => import('@/pages/admin/SystemMonitor.vue'),
        meta: {
          title: '系统监控',
          requiresAuth: true,
          requiresAdmin: true
        }
      }
    ]
  },

  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: NotFound,
    meta: { title: '页面未找到', requiresAuth: false }
  }
]

/**
 * 获取路由的完整标题
 */
export const getRouteTitle = (route: any): string => {
  const title = route.meta?.title || ''
  const appName = '东方炒炒币'
  
  if (title) {
    return `${title} - ${appName}`
  }
  
  return appName
}
