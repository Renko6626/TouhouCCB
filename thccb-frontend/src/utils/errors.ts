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

/** Loan POST transport failures can leave the committed outcome unknown. */
export function loanOperationError(err: unknown, fallback: string): string {
  const detail = extractErrorMessage(err, fallback)
  const failure = typeof err === 'object' && err !== null
    ? err as { status?: unknown; response?: { status?: unknown } }
    : null
  const hasResponse = failure?.status != null || failure?.response?.status != null
  if (!hasResponse && /timeout|timed out|超时|network error/i.test(detail)) {
    return '请求超时或连接中断，操作可能已执行。请刷新并核对账户后再决定是否重新提交。'
  }
  return detail
}
