import { ref } from 'vue'

/** 模块级单例：任何页面的 SSE snapshot 报告的服务端 build 与本地不一致时置 true。
 *  只提示不强刷——部署窗口内前端 rsync 先于后端重启完成，短暂 sha 不一致是正常态。 */
export const buildMismatch = ref(false)

export function reportServerBuild(sha: string | undefined): void {
  const mine = import.meta.env.VITE_BUILD_SHA
  if (!sha || !mine || sha === 'dev' || mine === 'dev') return
  if (sha !== mine) buildMismatch.value = true
}
