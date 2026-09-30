const SCALE = 1_000_000n

function decimal(value: string, label: string): bigint {
  if (!/^\d{1,10}(\.\d{1,6})?$/.test(value.trim())) throw new Error(`${label}请填写最多 6 位小数的非负数字`)
  const [whole = '0', fraction = ''] = value.trim().split('.')
  return BigInt(whole) * SCALE + BigInt(fraction.padEnd(6, '0'))
}

function format(value: bigint, places = 6): string {
  const scale = 10n ** BigInt(places)
  const fraction = (value % scale).toString().padStart(places, '0').replace(/0+$/, '')
  return `${value / scale}${fraction ? `.${fraction}` : ''}`
}

export function buildFxPairSetup(input: {
  price: string; gold: string; downPct: string; upPct: string; buyPct: string; sellPct: string
}) {
  const price = decimal(input.price, '开盘汇率')
  const gold = decimal(input.gold, '池子规模')
  const down = decimal(input.downPct, '下跌范围')
  const up = decimal(input.upPct, '上涨范围')
  const buy = decimal(input.buyPct, '买入手续费')
  const sell = decimal(input.sellPct, '卖出手续费')
  const hundred = 100n * SCALE
  if (price <= 0n || gold <= 0n) throw new Error('开盘汇率和池子规模必须大于 0')
  if (down >= hundred || buy >= hundred || sell >= hundred) throw new Error('下跌范围和手续费必须小于 100%')
  const foreign = gold * SCALE / price
  const min = price * (hundred - down) / hundred
  const max = price * (hundred + up) / hundred
  if (foreign <= 0n || min <= 0n) throw new Error('生成的储备或下限不足 0.000001，请增大规模或调整汇率范围')
  if (foreign >= 10_000_000_000n * SCALE || max >= 10_000_000_000n * SCALE) throw new Error('生成数值过大，请降低规模或汇率')
  return {
    gold_reserve: format(gold), foreign_reserve: format(foreign),
    initial_price: format(price), target_price: format(price), target_min: format(min), target_max: format(max),
    buy_fee_rate: format(buy, 8), sell_fee_rate: format(sell, 8),
  }
}
