import { compareFxAmounts, newFxIdempotencyKey } from './fx'
import type { FxBorrowBuyRequest } from '@/types/fx'

export interface FxPendingBorrowBuyRequest {
  readonly pairId: number
  readonly body: Readonly<FxBorrowBuyRequest>
}
type PendingStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
const PREFIX = 'fx-borrow-buy-pending-v1:'
function browserStorage(): PendingStorage | null {
  try { return typeof sessionStorage === 'undefined' ? null : sessionStorage }
  catch { return null }
}
function restore(value: unknown): FxPendingBorrowBuyRequest | null {
  if (!value || typeof value !== 'object') return null
  const request = value as Partial<FxPendingBorrowBuyRequest>
  const b = request.body
  const amount = (v: unknown): v is string => typeof v === 'string' && /^\d+(?:\.\d{0,6})?$/.test(v)
  if (!Number.isSafeInteger(request.pairId) || Number(request.pairId) <= 0 || !b
    || !amount(b.amount) || !amount(b.borrow_amount) || !amount(b.min_out)
    || compareFxAmounts(b.amount, '0') !== 1 || compareFxAmounts(b.borrow_amount, '0') !== 1
    || compareFxAmounts(b.borrow_amount, b.amount) === 1
    || typeof b.idempotency_key !== 'string' || !b.idempotency_key || b.idempotency_key.length > 128) return null
  return Object.freeze({ pairId: request.pairId!, body: Object.freeze({ ...b }) })
}

/** Store the wire identity before sending; an unknown outcome survives remounts. */
export class FxPendingBorrowBuyOrder {
  pending: FxPendingBorrowBuyRequest | null = null
  unreadable = false
  busy = false
  private corrupt = false
  private userId: string | null = null
  constructor(userId: string | number | null, private storage: PendingStorage | null = browserStorage()) {
    this.setUser(userId)
  }
  setUser(id: string | number | null) {
    const next = id == null ? null : String(id)
    if (next === this.userId) return
    this.userId = next
    this.pending = null
    this.unreadable = this.corrupt = false
    if (next === null) return
    if (!this.storage) { this.unreadable = true; return }
    try {
      const raw = this.storage.getItem(PREFIX + next)
      if (raw === null) return
      this.pending = restore(JSON.parse(raw))
      if (!this.pending) this.unreadable = this.corrupt = true
    } catch { this.unreadable = this.corrupt = true }
  }
  get hasUnresolved() { return this.corrupt || this.pending !== null }
  private clear(id: string, request: FxPendingBorrowBuyRequest) {
    try {
      const raw = this.storage?.getItem(PREFIX + id)
      const saved = raw ? restore(JSON.parse(raw)) : null
      if (raw && !saved) {
        if (id === this.userId) this.unreadable = this.corrupt = true
        return
      }
      if (saved && saved.body.idempotency_key !== request.body.idempotency_key) return
      this.storage?.removeItem(PREFIX + id)
      if (id === this.userId && this.pending?.body.idempotency_key === request.body.idempotency_key)
        this.pending = null
    } catch { if (id === this.userId) this.unreadable = this.corrupt = true }
  }
  async start<T>(pairId: number, body: Omit<FxBorrowBuyRequest, 'idempotency_key'>,
    run: (request: FxPendingBorrowBuyRequest) => Promise<T>): Promise<T | null> {
    if (this.busy || this.hasUnresolved) throw new Error('有融资订单等待确认，请先恢复原订单')
    if (!this.userId || !this.storage || this.unreadable) throw new Error('无法安全保存融资订单，请核对账户后重试')
    const request = restore({ pairId, body: { ...body, idempotency_key: newFxIdempotencyKey() } })
    if (!request) throw new Error('融资订单参数无效')
    try { this.storage.setItem(PREFIX + this.userId, JSON.stringify(request)) }
    catch { this.unreadable = true; throw new Error('无法安全保存融资订单，尚未提交') }
    this.pending = request
    return this.retry(run)
  }
  async retry<T>(run: (request: FxPendingBorrowBuyRequest) => Promise<T>): Promise<T | null> {
    if (this.busy || !this.pending || !this.userId) return null
    const request = this.pending, id = this.userId
    this.busy = true
    try {
      const result = await run(request)
      this.clear(id, request)
      return id === this.userId ? result : null
    } catch (error) {
      const e = error as { status?: number; response?: { status?: number } } | null
      const status = e?.response?.status ?? e?.status ?? 0
      // Authentication can fail before the server looks up a prior committed
      // request. Keep its identity until re-authentication confirms the result.
      if (status >= 400 && status < 500 && ![401, 403, 408, 429].includes(status)) this.clear(id, request)
      throw error
    } finally { this.busy = false }
  }
}
