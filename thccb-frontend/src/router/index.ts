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

// chunk load 失败自动 reload：SPA 部署后 vite 把 hash 文件名变了，server 上旧
// chunk 被删，开着旧 HTML 的用户点导航会 dynamic import 失败。reload 拉新 HTML
// + 新 chunk 后正常工作。Vue 社区标准做法。
router.onError((err) => {
  const msg = err?.message || ''
  if (/Loading chunk \w+ failed|Failed to fetch dynamically imported module|Importing a module script failed/i.test(msg)) {
    console.warn('[router] chunk load failed, reloading for new bundle:', msg)
    window.location.reload()
  }
})

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
