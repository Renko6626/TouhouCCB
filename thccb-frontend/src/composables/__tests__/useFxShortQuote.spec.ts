import { effectScope, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { FxShortQuote } from '@/types/fx'

vi.mock('@/api/fx', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/fx')>(),
  fxApi: { quoteShort: vi.fn() },
}))
import { fxApi } from '@/api/fx'
import { useFxShortQuote, type ShortQuoteOrder } from '../useFxShortQuote'

const quote: FxShortQuote = {
  pair_id: 1, action: 'open', purpose: 'short_open', requested_foreign_amount: '10',
  cover_all: false, actual_foreign_amount: '10', input_amount: '10', output_amount: '99',
  fee_amount: '1', fee_currency: 'foreign', restricted_gold_delta: '99',
  available_cash: '1000', affordable: true, estimated_equity: '999',
  estimated_risk_basis: '90', risk_status: 'ok', risk_blocked_reason: null,
  executable: true, blocked_reason: null, expires_at: '2030-01-01T00:00:00Z',
}
const scopes: ReturnType<typeof effectScope>[] = []
function setup() {
  vi.useFakeTimers()
  const order = ref<ShortQuoteOrder | null>({ pairId: 1, body: { action: 'open', foreign_amount: '10' } })
  const scope = effectScope()
  scopes.push(scope)
  return { order, resource: scope.run(() => useFxShortQuote(order))! }
}
afterEach(() => {
  scopes.splice(0).forEach(scope => scope.stop())
  vi.useRealTimers()
  vi.resetAllMocks()
})

describe('空头自动报价', () => {
  it('输入改变后立即移除旧报价，延迟旧响应不能覆盖新数量的报价', async () => {
    let resolveOld!: (value: FxShortQuote) => void
    vi.mocked(fxApi.quoteShort).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    const { order, resource } = setup()
    await vi.advanceTimersByTimeAsync(350)
    expect(resource.loading.value).toBe(true)
    vi.mocked(fxApi.quoteShort).mockResolvedValueOnce({ ...quote, requested_foreign_amount: '20' })
    order.value = { pairId: 1, body: { action: 'open', foreign_amount: '20' } }
    expect(resource.quote.value).toBeNull()
    await vi.advanceTimersByTimeAsync(350)
    expect(resource.quote.value?.requested_foreign_amount).toBe('20')
    resolveOld(quote)
    await vi.advanceTimersByTimeAsync(0)
    expect(resource.quote.value?.requested_foreign_amount).toBe('20')
  })

  it('存在待确认订单或离开表单时清除报价，旧响应无法重新启用提交', async () => {
    let resolve!: (value: FxShortQuote) => void
    vi.mocked(fxApi.quoteShort).mockImplementationOnce(() => new Promise(done => { resolve = done }))
    const { order, resource } = setup()
    await vi.advanceTimersByTimeAsync(350)
    expect(resource.loading.value).toBe(true)
    order.value = null
    resolve(quote)
    await vi.advanceTimersByTimeAsync(1000)
    expect(resource.quote.value).toBeNull()
    expect(resource.loading.value).toBe(false)
  })

  it('自动报价接受全回补请求，但拒绝返回其他币种的报价', async () => {
    const { order, resource } = setup()
    order.value = { pairId: 2, body: { action: 'cover', cover_all: true } }
    vi.mocked(fxApi.quoteShort).mockResolvedValueOnce({ ...quote, pair_id: 2, action: 'cover', cover_all: true, requested_foreign_amount: null })
    await vi.advanceTimersByTimeAsync(350)
    expect(resource.quote.value?.cover_all).toBe(true)
    vi.mocked(fxApi.quoteShort).mockResolvedValueOnce(quote)
    await resource.refresh()
    expect(resource.quote.value).toBeNull()
    expect(resource.error.value).toBe('报价与当前请求不一致，请重试')
  })
})
