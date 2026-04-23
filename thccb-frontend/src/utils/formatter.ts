/**
 * 数据格式化工具
 */

// 时间格式化
export function formatDate(date: Date | string, format: 'short' | 'medium' | 'long' = 'medium'): string {
  const options: Intl.DateTimeFormatOptions = {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  }

  if (format === 'medium') {
    options.hour = '2-digit'
    options.minute = '2-digit'
  } else if (format === 'long') {
    options.hour = '2-digit'
    options.minute = '2-digit'
    options.second = '2-digit'
  }

  return new Intl.DateTimeFormat('zh-CN', options).format(new Date(date))
}

// 相对时间（"刚刚 / X分钟前 / X小时前 / X天前 / YYYY-MM-DD"）
export function formatRelativeTime(input: string | Date | null | undefined): string {
  if (!input) return ''
  const ts = typeof input === 'string' ? new Date(input).getTime() : input.getTime()
  if (!Number.isFinite(ts)) return ''
  const diff = Date.now() - ts
  if (diff < 0) return '刚刚'
  const sec = Math.floor(diff / 1000)
  if (sec < 30) return '刚刚'
  if (sec < 60) return `${sec}秒前`
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min}分钟前`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr}小时前`
  const day = Math.floor(hr / 24)
  if (day < 30) return `${day}天前`
  return formatDate(new Date(ts), 'short')
}
