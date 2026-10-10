import { describe, expect, it } from 'vitest'
import { FxPendingBorrowBuyOrder } from '../fxBorrowBuy'

function memory() {
  const data = new Map<string, string>()
  return { getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => { data.set(key, value) },
    removeItem: (key: string) => { data.delete(key) } }
}
const body = { amount: '50', borrow_amount: '20', min_out: '40' }

describe('financed FX request recovery', () => {
  it('restores the exact identity after an unknown result and a page reload', async () => {
    const storage = memory()
    const first = new FxPendingBorrowBuyOrder(1, storage)
    await expect(first.start(7, body, async () => { throw new Error('connection lost') })).rejects.toThrow()
    const sent = first.pending
    const next = new FxPendingBorrowBuyOrder(1, storage)
    expect(next.pending).toEqual(sent)
    expect(new FxPendingBorrowBuyOrder(2, storage).pending).toBeNull()
    const result = await next.retry(async request => ({ key: request.body.idempotency_key }))
    expect(result?.key).toBe(sent?.body.idempotency_key)
    expect(new FxPendingBorrowBuyOrder(1, storage).pending).toBeNull()
  })

  it('blocks a second operation until a known rejection clears the old request', async () => {
    const order = new FxPendingBorrowBuyOrder(1, memory())
    await expect(order.start(7, body, async () => { throw { status: 503 } })).rejects.toEqual({ status: 503 })
    await expect(order.start(8, body, async () => 1)).rejects.toThrow()
    await expect(order.retry(async () => { throw { status: 400 } })).rejects.toEqual({ status: 400 })
    expect(order.pending).toBeNull()
    expect(await order.start(7, body, async () => 2)).toBe(2)
  })

  it('persists before sending and never sends when storage cannot be written', async () => {
    const storage = memory()
    storage.setItem = () => { throw new Error('unavailable') }
    const order = new FxPendingBorrowBuyOrder(1, storage)
    let sent = false
    await expect(order.start(7, body, async () => { sent = true })).rejects.toThrow()
    expect(sent).toBe(false)
    expect(order.unreadable).toBe(true)
  })

  it('a late result after account switching does not clear the other account request', async () => {
    const storage = memory()
    const order = new FxPendingBorrowBuyOrder(1, storage)
    let finish!: (value: number) => void
    const inFlight = order.start(7, body, () => new Promise<number>(resolve => { finish = resolve }))
    const second = new FxPendingBorrowBuyOrder(2, storage)
    await expect(second.start(8, body, async () => { throw new Error('lost') })).rejects.toThrow()
    const key = second.pending?.body.idempotency_key
    order.setUser(2)
    finish(1)
    expect(await inFlight).toBeNull()
    expect(order.pending?.body.idempotency_key).toBe(key)
    expect(new FxPendingBorrowBuyOrder(2, storage).pending?.body.idempotency_key).toBe(key)
  })
})
