import type { RouteLocationNormalized, NavigationGuardNext } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { getLoginUrl } from '@/api/casdoor'

export const globalBeforeEach = (
  to: RouteLocationNormalized,
  _from: RouteLocationNormalized,
  next: NavigationGuardNext,
) => {
  const authStore = useAuthStore()

  // 已登录用户访问 login/register 时跳首页
  if (to.matched.some((r) => r.meta.hideIfLoggedIn) && authStore.isAuthenticated) {
    return next('/')
  }

  // 需要登录的页面 → 直接跳转 Casdoor
  if (to.matched.some((r) => r.meta.requiresAuth) && !authStore.isAuthenticated) {
    window.location.href = getLoginUrl()
    return  // 中断导航，等待浏览器跳转
  }

  // 需要管理员权限的页面
  if (to.matched.some((r) => r.meta.requiresAdmin) && !authStore.isAdmin) {
    return next('/')
  }

  next()
}
