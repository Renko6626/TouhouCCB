import { onScopeDispose, ref, watch, type Ref } from 'vue'
import { compareFxAmounts, fxApi, mapFxError } from '@/api/fx'
import type { FxShortQuote, FxShortQuoteRequest } from '@/types/fx'

export interface ShortQuoteOrder {
  pairId: number
  body: FxShortQuoteRequest
}

/** Passing null immediately invalidates a quote, including any in-flight reply. */
export function useFxShortQuote(order: Ref<ShortQuoteOrder | null>) {
  const quote = ref<FxShortQuote | null>(null)
  const loading = ref(false)
  const error = ref('')
  let generation = 0
  let timer: ReturnType<typeof setTimeout> | null = null
  let disposed = false

  function clearTimer() {
    if (timer) clearTimeout(timer)
    timer = null
  }

  async function refresh() {
    clearTimer()
    const request = order.value
    if (!request || disposed) return
    const current = ++generation
    quote.value = null
    error.value = ''
    loading.value = true
    try {
      const result = await fxApi.quoteShort(request.pairId, request.body)
      if (current !== generation || disposed) return
      if (result.pair_id !== request.pairId || result.action !== request.body.action
        || !!result.cover_all !== !!request.body.cover_all
        || (request.body.foreign_amount !== undefined
          && compareFxAmounts(result.requested_foreign_amount, request.body.foreign_amount) !== 0)) {
        error.value = '报价与当前请求不一致，请重试'
      } else {
        quote.value = result
      }
    } catch (e) {
      if (current === generation && !disposed) error.value = mapFxError(e, '空头报价失败')
    } finally {
      if (current === generation && !disposed) loading.value = false
    }
  }

  watch(order, () => {
    clearTimer()
    generation++
    quote.value = null
    error.value = ''
    loading.value = false
    if (order.value) timer = setTimeout(() => { void refresh() }, 350)
  }, { immediate: true, flush: 'sync' })

  onScopeDispose(() => {
    disposed = true
    generation++
    clearTimer()
  })
  return { quote, loading, error, refresh }
}
