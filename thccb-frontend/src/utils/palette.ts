/**
 * 涨跌/盈亏色板的 TS 读取层。
 *
 * 真实数据源：src/assets/base.css 中的 --color-up / --color-down 等 CSS 变量。
 * 图表库（lightweight-charts、ECharts）只接受 hex 字符串，不能直接吃 var()，
 * 所以这里在运行时把 CSS 变量解析为字符串，让图表颜色自动跟随全局色板。
 *
 * 约定：绿=涨 / 浮盈，红=跌 / 浮亏。
 */

const FALLBACK = {
  up: '#16a34a',
  down: '#dc2626',
  upBg: '#f0fdf4',
  downBg: '#fef2f2',
  upStrong: '#4ade80',
  downStrong: '#ff6b6b',
  neutral: '#000000',
} as const

const readVar = (name: string, fallback: string): string => {
  if (typeof window === 'undefined') return fallback
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

/**
 * 获取当前色板的 hex 值。每次调用重新读取，便于热更 / 主题切换。
 */
export function getPalette() {
  return {
    up: readVar('--color-up', FALLBACK.up),
    down: readVar('--color-down', FALLBACK.down),
    upBg: readVar('--color-up-bg', FALLBACK.upBg),
    downBg: readVar('--color-down-bg', FALLBACK.downBg),
    upStrong: readVar('--color-up-strong', FALLBACK.upStrong),
    downStrong: readVar('--color-down-strong', FALLBACK.downStrong),
    neutral: FALLBACK.neutral,
  }
}

/** 在 6 位 hex 后追加 alpha（0-255）转为 8 位 hex。例：withAlpha('#16a34a', 0x80) */
export function withAlpha(hex6: string, alphaByte: number): string {
  const a = Math.max(0, Math.min(255, alphaByte)).toString(16).padStart(2, '0')
  return `${hex6}${a}`
}
