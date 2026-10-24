import type { RouteLocationNormalized, NavigationGuardNext } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { getLoginUrl } from '@/api/casdoor'
import { siteAccessApi } from '@/api/siteAccess'

export const globalBeforeEach = async (
  to: RouteLocationNormalized,
  _from: RouteLocationNormalized,
  next: NavigationGuardNext,
) => {
  const authStore = useAuthStore()

  // The backend remains authoritative; never trust a cached browser administrator role.
  if (!['access-denied', 'login', 'auth-callback'].includes(String(to.name))) {
    try {
      const { disabled } = await siteAccessApi.status()
      if (disabled) {
        const user = await authStore.fetchCurrentUser()
        if (!user?.is_superuser) return next('/access-denied')
      }
    } catch {
      // Network failures still reach the normal error UI; business APIs enforce the gate.
    }
  }

  // 已登录用户访问 login/register 时跳首页
  if (to.matched.some((r) => r.meta.hideIfLoggedIn) && authStore.isAuthenticated
      && !(to.name === 'login' && to.query.admin === '1')) {
    return next('/')
  }

  // 需要登录的页面 → 直接跳转 Casdoor
  if (to.matched.some((r) => r.meta.requiresAuth) && !authStore.isAuthenticated) {
    window.location.href = await getLoginUrl()
    return  // 中断导航，等待浏览器跳转
  }

  // 需要管理员权限的页面
  if (to.matched.some((r) => r.meta.requiresAdmin) && !authStore.isAdmin) {
    return next('/')
  }

  next()
}
