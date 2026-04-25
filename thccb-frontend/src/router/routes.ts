import type { RouteRecordRaw } from 'vue-router'
import DefaultLayout from '@/layouts/DefaultLayout.vue'
import AuthLayout from '@/layouts/AuthLayout.vue'
import NotFound from '@/pages/NotFound.vue'

/**
 * 路由定义
 * 顶层用 layout 组件作为 component，子路由通过 layout 内部的 <router-view /> 渲染。
 */
export const routes: RouteRecordRaw[] = [
  // 主布局：Header + Sidebar + Footer
  {
    path: '/',
    component: DefaultLayout,
    children: [
      {
        path: '',
        name: 'home',
        component: () => import('@/pages/home/Home.vue'),
        meta: {
          title: '首页',
          requiresAuth: false
        }
      },

      // 用户路由
      {
        path: 'user',
        redirect: '/user/portfolio',
      },
      {
        path: 'user/portfolio',
        name: 'portfolio',
        component: () => import('@/pages/user/Portfolio.vue'),
        meta: {
          title: '我的资产',
          requiresAuth: true,
          requiresVerified: true
        }
      },
      {
        path: 'user/transactions',
        name: 'transactions',
        component: () => import('@/pages/user/Transactions.vue'),
        meta: {
          title: '交易记录',
          requiresAuth: true,
          requiresVerified: true
        }
      },
      {
        path: 'loan',
        name: 'loan',
        component: () => import('@/pages/loan/Loan.vue'),
        meta: {
          title: '借款',
          requiresAuth: true,
          requiresVerified: true,
        },
      },

      // 兑换中心
      {
        path: 'redemption',
        name: 'redemption-list',
        component: () => import('@/pages/redemption/RedemptionList.vue'),
        meta: { title: '兑换中心', requiresAuth: true, requiresVerified: true },
      },
      {
        path: 'redemption/batches/:id',
        name: 'redemption-batch-detail',
        component: () => import('@/pages/redemption/BatchDetail.vue'),
        meta: { title: '批次详情', requiresAuth: true, requiresVerified: true },
      },
      {
        path: 'my/redemptions',
        name: 'my-redemptions',
        component: () => import('@/pages/user/MyRedemptions.vue'),
        meta: { title: '我的兑换', requiresAuth: true, requiresVerified: true },
      },

      // 市场路由
      {
        path: 'market',
        redirect: '/market/list',
      },
      {
        path: 'market/list',
        name: 'market-list',
        component: () => import('@/pages/market/MarketList.vue'),
        meta: {
          title: '市场列表',
          requiresAuth: true,
          requiresVerified: true
        }
      },
      {
        path: 'market/:id/trade',
        name: 'trading-view',
        component: () => import('@/pages/market/TradingView.vue'),
        meta: {
          title: '交易视图',
          requiresAuth: true,
          requiresVerified: true
        }
      },
      {
        path: 'market/leaderboard',
        name: 'leaderboard',
        component: () => import('@/pages/market/Leaderboard.vue'),
        meta: {
          title: '财富排行榜',
          requiresAuth: true,
          requiresVerified: true
        }
      },

      // 管理员路由
      {
        path: 'admin',
        redirect: '/admin/market-manage',
      },
      {
        path: 'admin/market-manage',
        name: 'market-manage',
        component: () => import('@/pages/admin/MarketManage.vue'),
        meta: {
          title: '管理后台',
          requiresAuth: true,
          requiresAdmin: true
        }
      },
      {
        path: 'admin/site-config',
        name: 'admin-site-config',
        component: () => import('@/pages/admin/SiteConfig.vue'),
        meta: {
          title: '站点配置',
          requiresAuth: true,
          requiresAdmin: true,
        },
      },
      {
        path: 'admin/redemption/partners',
        name: 'admin-redemption-partners',
        component: () => import('@/pages/admin/RedemptionPartners.vue'),
        meta: { title: '合作方管理', requiresAuth: true, requiresAdmin: true },
      },
      {
        path: 'admin/redemption/batches',
        name: 'admin-redemption-batches',
        component: () => import('@/pages/admin/RedemptionBatches.vue'),
        meta: { title: '兑换批次', requiresAuth: true, requiresAdmin: true },
      },
      {
        path: 'admin/redemption/batches/:id/import',
        name: 'admin-redemption-import',
        component: () => import('@/pages/admin/RedemptionImport.vue'),
        meta: { title: '导入兑换码', requiresAuth: true, requiresAdmin: true },
      },

      // 404 也走主布局，方便用户从导航返回
      {
        path: ':pathMatch(.*)*',
        name: 'not-found',
        component: NotFound,
        meta: { title: '页面未找到', requiresAuth: false }
      }
    ]
  },

  // 认证布局：独立居中卡片，无 Header
  {
    path: '/auth',
    component: AuthLayout,
    children: [
      {
        path: '',
        redirect: '/auth/login',
      },
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
      },
      {
        path: 'callback',
        name: 'auth-callback',
        component: () => import('@/pages/auth/Callback.vue'),
        meta: { title: '登录中', requiresAuth: false },
      },
    ]
  },
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
