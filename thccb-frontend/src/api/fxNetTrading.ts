import { FxPendingOrder, type FxPendingStorage } from './fxPendingOrder'
import api from './index'
import { compareFxAmounts, newFxIdempotencyKey } from './fx'
import type { FxNetTradeRequest } from '@/types/fx'

export interface FxPendingNetRequest {
  readonly pairId: number
  readonly body: Readonly<FxNetTradeRequest>
}
function restore(value: unknown): FxPendingNetRequest | null {
  if (!value || typeof value !== 'object') return null
  const request = value as Partial<FxPendingNetRequest>
  const b = request.body
  const amount = (v: unknown): v is string => typeof v === 'string' && /^\d+(?:\.\d{0,6})?$/.test(v)
  if (
    !Number.isSafeInteger(request.pairId) ||
    Number(request.pairId) <= 0 ||
    !b ||
    !amount(b.amount) ||
    !amount(b.borrow_amount) ||
    !amount(b.min_out) ||
    compareFxAmounts(b.amount, '0') !== 1 ||
    compareFxAmounts(b.borrow_amount, '0') === -1 ||
    compareFxAmounts(b.borrow_amount, b.amount) === 1 ||
    typeof b.allow_growth !== 'boolean' ||
    !['buy', 'sell'].includes(b.side) ||
    (b.side === 'sell' && compareFxAmounts(b.borrow_amount, '0') !== 0) ||
    !Number.isSafeInteger(b.expected_economic_version) ||
    b.expected_economic_version < 0 ||
    typeof b.idempotency_key !== 'string' ||
    !b.idempotency_key ||
    b.idempotency_key.length > 128
  )
    return null
  return Object.freeze({ pairId: request.pairId!, body: Object.freeze({ ...b }) })
}

/** Wire-specific adapter; persistence and recovery share one policy. */
export class FxPendingNetOrder extends FxPendingOrder<FxPendingNetRequest> {
  constructor(userId: string | number | null, storage?: FxPendingStorage | null) {
    super(userId, 'fx-net-pending-v1:', restore, storage)
  }
  start<T>(
    pairId: number,
    body: Omit<FxNetTradeRequest, 'idempotency_key'>,
    run: (request: FxPendingNetRequest) => Promise<T>,
  ): Promise<T | null> {
    return this.submit({ pairId, body: { ...body, idempotency_key: newFxIdempotencyKey() } }, run)
  }
}

import type { FxNetQuote, FxNetQuoteRequest, FxNetTrade } from '@/types/fx'
export const fxNetApi = {
  quote(pairId: number, body: FxNetQuoteRequest): Promise<FxNetQuote> {
    return api.post(`/api/v1/fx/pairs/${pairId}/net/quote`, body)
  },
  trade(pairId: number, body: FxNetTradeRequest): Promise<FxNetTrade> {
    return api.post(`/api/v1/fx/pairs/${pairId}/net/trades`, body)
  },
}

/** Convert the legacy writer version without silent precision loss. */
export function safeEconomicVersion(version: string | undefined): number | null {
  if (!version || !/^\d+$/.test(version)) return null
  const value = BigInt(version)
  return value <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(value) : null
}
