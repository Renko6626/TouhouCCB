import { onScopeDispose, ref, watch, type Ref } from 'vue'
import { compareFxAmounts, fxApi, mapFxError } from '@/api/fx'
import type { FxBorrowBuyQuote, FxBorrowBuyQuoteRequest } from '@/types/fx'

export interface BorrowBuyQuoteOrder { pairId: number; body: FxBorrowBuyQuoteRequest }
export function useFxBorrowBuyQuote(order: Ref<BorrowBuyQuoteOrder | null>) {
  const quote = ref<FxBorrowBuyQuote | null>(null), loading = ref(false), error = ref('')
  let generation = 0, disposed = false
  let timer: ReturnType<typeof setTimeout> | null = null
  function clearTimer() { if (timer) clearTimeout(timer); timer = null }
  async function refresh() {
    clearTimer()
    const request = order.value
    if (!request || disposed) return
    const current = ++generation
    quote.value = null; error.value = ''; loading.value = true
    try {
      const result = await fxApi.quoteBorrowBuy(request.pairId, request.body)
      if (current !== generation || disposed) return
      if (result.pair_id !== request.pairId
        || compareFxAmounts(result.input_amount, request.body.amount) !== 0
        || compareFxAmounts(result.borrow_amount, request.body.borrow_amount) !== 0)
        error.value = '报价与融资金额不一致，请重新报价'
      else quote.value = result
    } catch (e) {
      if (current === generation && !disposed) error.value = mapFxError(e, '融资报价失败')
    } finally { if (current === generation && !disposed) loading.value = false }
  }
  watch(order, () => {
    clearTimer(); generation++; quote.value = null; error.value = ''; loading.value = false
    if (order.value) timer = setTimeout(() => { void refresh() }, 350)
  }, { immediate: true, flush: 'sync' })
  onScopeDispose(() => { disposed = true; generation++; clearTimer() })
  return { quote, loading, error, refresh }
}
