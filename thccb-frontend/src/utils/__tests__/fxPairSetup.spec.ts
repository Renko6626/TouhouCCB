import { describe, expect, it } from 'vitest'
import { buildFxPairSetup } from '../fxPairSetup'

const input = { price: '0.2', gold: '1000000', downPct: '20', upPct: '30', buyPct: '0.2', sellPct: '0.2' }

describe('FX 快捷配置', () => {
  it('根据汇率与池子规模生成储备和百分比参数', () => {
    expect(buildFxPairSetup(input)).toEqual({
      gold_reserve: '1000000', foreign_reserve: '5000000', initial_price: '0.2',
      target_price: '0.2', target_min: '0.16', target_max: '0.26',
      buy_fee_rate: '0.002', sell_fee_rate: '0.002',
    })
  })
  it('保留六位金额精度并对除不尽的储备向下取整', () => {
    expect(buildFxPairSetup({ ...input, price: '3', gold: '1' }).foreign_reserve).toBe('0.333333')
  })
  it.each([{ price: '0' }, { gold: '-1' }, { downPct: '100' }, { buyPct: '100' }, { price: '' }, { gold: 'Infinity' }])('拒绝无效输入 %o', change => {
    expect(() => buildFxPairSetup({ ...input, ...change })).toThrow()
  })
})
