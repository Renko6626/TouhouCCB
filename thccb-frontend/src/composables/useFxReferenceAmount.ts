import { ref, watch, type Ref } from 'vue'
import { compareFxAmounts, divideFxAmount, multiplyFxAmount } from '@/api/fx'

/** The scale is a reference gold value; the executable order remains a foreign quantity. */
export function useFxReferenceAmount(quantity: Ref<string>, price: Readonly<Ref<string | null>>) {
  const goldAmount = ref('')
  let editingGold = false
  watch(
    [quantity, price],
    () => {
      if (!editingGold)
        goldAmount.value =
          quantity.value && price.value ? (multiplyFxAmount(quantity.value, price.value) ?? '') : ''
    },
    { immediate: true, flush: 'sync' },
  )
  function updateGoldAmount(value: string) {
    goldAmount.value = value
    const converted =
      /^\d+(\.\d{0,6})?$/.test(value.trim()) && compareFxAmounts(price.value, '0') === 1
        ? divideFxAmount(value, price.value, 6)
        : null
    editingGold = true
    quantity.value = compareFxAmounts(converted, '0') === 1 ? converted! : ''
    editingGold = false
  }
  return { goldAmount, updateGoldAmount }
}
