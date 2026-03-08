import type { RouteLocationNormalized, NavigationGuardNext } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

/**
 * 认证守卫 - 检查用户是否已登录
 */
export const authGuard = (
  to: RouteLocationNormalized,
  from: RouteLocationNormalized,
  next: NavigationGuardNext
) => {
  const authStore = useAuthStore()
  
  // 检查路由是否需要认证
  const requiresAuth = to.matched.some(record => record.meta.requiresAuth)
  
  if (requiresAuth && !authStore.isAuthenticated) {
    // 如果需要认证但用户未登录，重定向到登录页
    next({
      path: '/auth/login',
      query: { redirect: to.fullPath }
    })
  } else {
    next()
  }
}

/**
 * 管理员权限守卫 - 检查用户是否有管理员权限
 */
export const adminGuard = (
  to: RouteLocationNormalized,
  from: RouteLocationNormalized,
  next: NavigationGuardNext
) => {
  const authStore = useAuthStore()
  
  // 检查路由是否需要管理员权限
  const requiresAdmin = to.matched.some(record => record.meta.requiresAdmin)
  
  if (requiresAdmin && !authStore.isAdmin) {
    // 如果需要管理员权限但用户不是管理员，重定向到首页或显示无权限页面
    next({ path: '/' })
  } else {
    next()
  }
}

/**
 * 已验证账号守卫 - 检查用户账号是否已激活
 */
export const verifiedGuard = (
  to: RouteLocationNormalized,
  from: RouteLocationNormalized,
  next: NavigationGuardNext
) => {
  const authStore = useAuthStore()
  
  // 检查路由是否需要已验证账号
  const requiresVerified = to.matched.some(record => record.meta.requiresVerified)
  
  if (requiresVerified && !authStore.isVerified) {
    // 如果需要已验证账号但用户未激活，重定向到激活页面
    next({ path: '/auth/activate' })
  } else {
    next()
  }
}

/**
 * 已登录用户重定向守卫 - 如果用户已登录，重定向到首页
 * 用于登录、注册等页面
 */
export const loggedInRedirectGuard = (
  to: RouteLocationNormalized,
  from: RouteLocationNormalized,
  next: NavigationGuardNext
) => {
  const authStore = useAuthStore()
  
  // 检查路由是否应该对已登录用户不可访问
  const hideIfLoggedIn = to.matched.some(record => record.meta.hideIfLoggedIn)
  
  if (hideIfLoggedIn && authStore.isAuthenticated) {
    // 如果用户已登录，重定向到首页
    next({ path: '/' })
  } else {
    next()
  }
}

/**
 * 全局前置守卫 - 组合所有守卫
 */
export const globalBeforeEach = (
  to: RouteLocationNormalized,
  from: RouteLocationNormalized,
  next: NavigationGuardNext
) => {
  // 按顺序执行守卫
  loggedInRedirectGuard(to, from, () => {
    authGuard(to, from, () => {
      verifiedGuard(to, from, () => {
        adminGuard(to, from, next)
      })
    })
  })
}