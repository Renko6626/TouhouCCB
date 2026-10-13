import { FxPendingOrder, type FxPendingStorage } from './fxPendingOrder'
import { compareFxAmounts, newFxIdempotencyKey } from './fx'
import type { FxBorrowBuyRequest } from '@/types/fx'

export interface FxPendingBorrowBuyRequest {
  readonly pairId: number
  readonly body: Readonly<FxBorrowBuyRequest>
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

/** Wire-specific adapter; persistence and recovery share one policy. */
export class FxPendingBorrowBuyOrder extends FxPendingOrder<FxPendingBorrowBuyRequest> {
  constructor(userId: string | number | null, storage?: FxPendingStorage | null) {
    super(userId, 'fx-borrow-buy-pending-v1:', restore, storage)
  }
  start<T>(
    pairId: number,
    body: Omit<FxBorrowBuyRequest, 'idempotency_key'>,
    run: (request: FxPendingBorrowBuyRequest) => Promise<T>,
  ): Promise<T | null> {
    return this.submit({ pairId, body: { ...body, idempotency_key: newFxIdempotencyKey() } }, run)
  }
}
