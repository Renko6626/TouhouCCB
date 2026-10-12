import { ref, shallowRef } from 'vue'
export interface FxBorrowConfirmationDetails {
  kind: 'long' | 'short'
  currency: string
  borrowAmount: string
  cashInput: string | null
  quantity: string
  lockedGold: string | null
  equity: string | null
  basis: string | null
  maintenance: string | null
  reliable: boolean
}
interface Confirmation {
  details: FxBorrowConfirmationDetails
  key: string
  expiresAt: number
  execute: () => Promise<void>
}
/** Capture the submitted amounts and price protection; accepting never rebuilds an order. */
export function useFxBorrowConfirmation(
  getKey: () => string,
  enabled: () => boolean,
  disable: () => void,
) {
  const pending = shallowRef<Confirmation | null>(null)
  const error = ref('')
  async function request(
    details: FxBorrowConfirmationDetails,
    expiresAt: number,
    execute: () => Promise<void>,
  ) {
    error.value = ''
    pending.value = { details, key: getKey(), expiresAt, execute }
    if (!enabled()) await confirm(false)
  }
  async function confirm(dontShowAgain: boolean) {
    const captured = pending.value
    if (!captured) return
    pending.value = null
    if (captured.key !== getKey()) {
      error.value = '订单已变化，请重新下单确认。'
      return
    }
    if (Date.now() >= captured.expiresAt) {
      error.value = '报价已过期，请重新报价后确认。'
      return
    }
    if (dontShowAgain) disable()
    await captured.execute()
  }
  function cancel() {
    pending.value = null
  }
  return { pending, error, request, confirm, cancel }
}
