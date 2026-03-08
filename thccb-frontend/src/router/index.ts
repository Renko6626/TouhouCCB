import { createRouter, createWebHistory } from 'vue-router'
import { routes } from './routes'
import { globalBeforeEach } from './guards'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
  scrollBehavior(to, from, savedPosition) {
    if (savedPosition) {
      return savedPosition
    } else {
      return { top: 0 }
    }
  },
})

// 设置全局前置守卫
router.beforeEach(globalBeforeEach)

// 设置页面标题
router.afterEach((to) => {
  const title = to.meta?.title as string | undefined
  const appName = '东方炒炒币'
  
  if (title) {
    document.title = `${title} - ${appName}`
  } else {
    document.title = appName
  }
})

export default router
