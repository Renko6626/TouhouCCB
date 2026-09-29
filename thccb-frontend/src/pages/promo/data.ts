import type { UTCTimestamp } from 'lightweight-charts'

export const demoOutcomes = [
  {
    id: 'reimu',
    name: '博丽灵梦',
    code: 'REIMU',
    title: '乐园的巫女',
    price: 0.2864,
    change: 12.68,
    shares: 8000,
    cost: 0.2412,
  },
  {
    id: 'marisa',
    name: '雾雨魔理沙',
    code: 'MARISA',
    title: '普通的魔法使',
    price: 0.2248,
    change: 8.32,
    shares: 6000,
    cost: 0.2075,
  },
  {
    id: 'sakuya',
    name: '十六夜咲夜',
    code: 'SAKUYA',
    title: '完全潇洒的女仆',
    price: 0.1826,
    change: -3.24,
    shares: 4500,
    cost: 0.1948,
  },
  {
    id: 'remilia',
    name: '蕾米莉亚',
    code: 'REMILIA',
    title: '永远鲜红的幼月',
    price: 0.1642,
    change: 5.76,
    shares: 0,
    cost: 0.1553,
  },
  {
    id: 'koishi',
    name: '古明地恋',
    code: 'KOISHI',
    title: '紧闭的恋之瞳',
    price: 0.142,
    change: -1.86,
    shares: 0,
    cost: 0.1447,
  },
]

export type DemoOutcome = (typeof demoOutcomes)[number]
export type DemoInterval = '1m' | '5m' | '15m' | '1h'
export const intervalSeconds: Record<DemoInterval, number> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '1h': 3600,
}

// 固定种子与时间让每次截图一致；所有价格均为虚拟金圆券数值。
export function makeDemoCandles(outcome: DemoOutcome, interval: DemoInterval) {
  let seed = demoOutcomes.findIndex((item) => item.id === outcome.id) + 42
  const random = () => {
    seed = (seed * 16807) % 2147483647
    return (seed - 1) / 2147483646
  }
  const count = 100
  const step = intervalSeconds[interval]
  const end = Date.UTC(2026, 4, 23, 7, 0) / 1000
  const positive = outcome.change > 0
  const raw = Array.from({ length: count }, (_, index) => {
    const progress = index / (count - 1)
    const trend = positive ? 0.74 + progress * 0.26 : 1.1 - progress * 0.1
    const wave = Math.sin(index * 0.17) * 0.04 + Math.sin(index * 0.49) * 0.017
    return outcome.price * (trend + wave + (random() - 0.5) * 0.032)
  })
  const scale = outcome.price / raw[count - 1]!
  return raw.map((value, index) => {
    const close = index === count - 1 ? outcome.price : value * scale
    const open = index === 0 ? close * 0.992 : raw[index - 1]! * scale
    return {
      time: (end - (count - 1 - index) * step) as UTCTimestamp,
      open,
      close,
      high: Math.max(open, close) + outcome.price * (0.004 + random() * 0.018),
      low: Math.min(open, close) - outcome.price * (0.004 + random() * 0.018),
      volume: Math.round(1800 + random() * 9500 + Math.abs(close - open) * 900000),
    }
  })
}

export function movingAverage(candles: ReturnType<typeof makeDemoCandles>, period: number) {
  return candles.slice(period - 1).map((candle, index) => ({
    time: candle.time,
    value: candles.slice(index, index + period).reduce((sum, item) => sum + item.close, 0) / period,
  }))
}

export const initialTrades = [
  { time: '15:00:28', name: '博丽灵梦', side: 'buy', price: 0.2864, shares: 1280 },
  { time: '15:00:24', name: '雾雨魔理沙', side: 'buy', price: 0.2248, shares: 650 },
  { time: '15:00:19', name: '十六夜咲夜', side: 'sell', price: 0.1826, shares: 420 },
  { time: '15:00:16', name: '博丽灵梦', side: 'buy', price: 0.2856, shares: 2100 },
  { time: '15:00:12', name: '古明地恋', side: 'sell', price: 0.142, shares: 860 },
  { time: '15:00:08', name: '蕾米莉亚', side: 'buy', price: 0.1642, shares: 1500 },
]
