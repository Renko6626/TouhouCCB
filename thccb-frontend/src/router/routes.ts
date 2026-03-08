import type { RouteRecordRaw } from 'vue-router'

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
      }
    ]
  },

  // 404页面 - 使用简单的组件
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: {
      template: `
        <div class="min-h-screen flex items-center justify-center">
          <div class="text-center">
            <h1 class="text-4xl font-bold text-gray-800 dark:text-gray-200 mb-4">404</h1>
            <p class="text-gray-600 dark:text-gray-400 mb-6">页面未找到</p>
            <button 
              @click="$router.push('/')"
              class="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700"
            >
              返回首页
            </button>
          </div>
        </div>
      `
    },
    meta: {
      title: '页面未找到',
      requiresAuth: false
    }
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
