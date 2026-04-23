/**
 * 数据格式化工具
 * 提供常用的数据格式化功能
 */

// 金额格式化（人民币）
export function formatCurrency(amount: number): string {
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency: 'CNY',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  }).format(amount)
}

// 百分比格式化
export function formatPercentage(value: number): string {
  return new Intl.NumberFormat('zh-CN', {
    style: 'percent',
    minimumFractionDigits: 1,
    maximumFractionDigits: 1
  }).format(value / 100)
}

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

// 数字格式化（千分位）
export function formatNumber(num: number): string {
  return new Intl.NumberFormat('zh-CN').format(num)
}

// 市场状态格式化
export function formatMarketStatus(status: string): string {
  const statusMap: Record<string, string> = {
    'trading': '交易中',
    'halt': '熔断中',
    'settled': '已结算'
  }
  return statusMap[status] || status
}

// 股票代码格式化（模拟）
export function formatStockCode(code: string): string {
  return code.toUpperCase()
}

// 精度控制
export function roundToPrecision(value: number, precision: number = 2): number {
  const factor = Math.pow(10, precision)
  return Math.round(value * factor) / factor
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