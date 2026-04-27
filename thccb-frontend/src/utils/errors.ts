/**
 * 从 axios 拦截器抛出的 reject 对象（{ message, status, data }）
 * 或其他 unknown 异常中提取可展示的错误文案。
 */
export function extractErrorMessage(err: unknown, fallback = '请求失败'): string {
  if (typeof err === 'object' && err !== null) {
    const e = err as { data?: { detail?: unknown }; message?: unknown }
    if (typeof e.data?.detail === 'string') return e.data.detail
    if (typeof e.message === 'string') return e.message
  }
  return fallback
}
