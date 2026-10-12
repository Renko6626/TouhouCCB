import { effectScope, ref } from 'vue'
import { expect, it } from 'vitest'
import { useFxReferenceAmount } from '../useFxReferenceAmount'
it('converts the sell scale to the real foreign order quantity and follows quantity or price changes', () => {
  const scope = effectScope(),
    quantity = ref(''),
    price = ref<string | null>('2')
  const scale = scope.run(() => useFxReferenceAmount(quantity, price))!
  scale.updateGoldAmount('500')
  expect(quantity.value).toBe('250.000000')
  expect(scale.goldAmount.value).toBe('500')
  quantity.value = '10'
  expect(scale.goldAmount.value).toBe('20.000000')
  price.value = '3'
  expect(quantity.value).toBe('10')
  expect(scale.goldAmount.value).toBe('30.000000')
  scope.stop()
})
it('does not retain an executable old quantity for invalid input or an unavailable reference price', () => {
  const scope = effectScope(),
    quantity = ref('10'),
    price = ref<string | null>('2')
  const scale = scope.run(() => useFxReferenceAmount(quantity, price))!
  scale.updateGoldAmount('abc')
  expect(quantity.value).toBe('')
  expect(scale.goldAmount.value).toBe('abc')
  price.value = null
  scale.updateGoldAmount('100')
  expect(quantity.value).toBe('')
  price.value = '2'
  scale.updateGoldAmount('0.000001')
  expect(quantity.value).toBe('')
  scope.stop()
})
