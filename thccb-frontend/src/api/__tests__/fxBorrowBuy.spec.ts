import { describe, expect, it } from 'vitest'
import { FxPendingBorrowBuyOrder } from '../fxBorrowBuy'
import { FxPendingNetOrder, safeEconomicVersion } from '../fxNetTrading'

function memory() {
  const data = new Map<string, string>()
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value)
    },
    removeItem: (key: string) => {
      data.delete(key)
    },
  }
}
describe.each([
  {
    name: 'legacy financed FX',
    Order: FxPendingBorrowBuyOrder,
    body: {
      amount: '50',
      borrow_amount: '20',
      min_out: '40',
      side: 'buy' as const,
      expected_economic_version: 7,
      allow_growth: true,
    },
  },
  {
    name: 'net FX',
    Order: FxPendingNetOrder,
    body: {
      amount: '50',
      borrow_amount: '0',
      min_out: '40',
      side: 'sell' as const,
      expected_economic_version: 7,
      allow_growth: true,
    },
  },
])('$name request recovery', ({ Order, body }) => {
  it('restores the exact identity after an unknown result and a page reload', async () => {
    const storage = memory()
    const first = new Order(1, storage)
    await expect(
      first.start(7, body, async () => {
        throw new Error('connection lost')
      }),
    ).rejects.toThrow()
    const sent = first.pending
    const next = new Order(1, storage)
    expect(next.pending).toEqual(sent)
    expect(new Order(2, storage).pending).toBeNull()
    const result = await next.retry(async (request) => ({ key: request.body.idempotency_key }))
    expect(result?.key).toBe(sent?.body.idempotency_key)
    expect(new Order(1, storage).pending).toBeNull()
  })

  it('blocks a second operation until a known rejection clears the old request', async () => {
    const order = new Order(1, memory())
    await expect(
      order.start(7, body, async () => {
        throw { status: 503 }
      }),
    ).rejects.toEqual({ status: 503 })
    await expect(order.start(8, body, async () => 1)).rejects.toThrow()
    await expect(
      order.retry(async () => {
        throw { status: 400 }
      }),
    ).rejects.toEqual({ status: 400 })
    expect(order.pending).toBeNull()
    expect(await order.start(7, body, async () => 2)).toBe(2)
  })

  it('persists before sending and never sends when storage cannot be written', async () => {
    const storage = memory()
    storage.setItem = () => {
      throw new Error('unavailable')
    }
    const order = new Order(1, storage)
    let sent = false
    await expect(
      order.start(7, body, async () => {
        sent = true
      }),
    ).rejects.toThrow()
    expect(sent).toBe(false)
    expect(order.storageError).toBe(true)
  })

  it('a late result after account switching does not clear the other account request', async () => {
    const storage = memory()
    const order = new Order(1, storage)
    let finish!: (value: number) => void
    const inFlight = order.start(
      7,
      body,
      () =>
        new Promise<number>((resolve) => {
          finish = resolve
        }),
    )
    const second = new Order(2, storage)
    await expect(
      second.start(8, body, async () => {
        throw new Error('lost')
      }),
    ).rejects.toThrow()
    const key = second.pending?.body.idempotency_key
    order.setUser(2)
    finish(1)
    expect(await inFlight).toBeNull()
    expect(order.pending?.body.idempotency_key).toBe(key)
    expect(new Order(2, storage).pending?.body.idempotency_key).toBe(key)
  })
})


describe('net writer admission identity', () => {
  it('rejects versions that cannot be represented by the legacy JSON integer', () => {
    expect(safeEconomicVersion('9007199254740991')).toBe(9007199254740991)
    expect(safeEconomicVersion('9007199254740992')).toBeNull()
    expect(safeEconomicVersion('7.1')).toBeNull()
    expect(safeEconomicVersion('-1')).toBeNull()
  })
  it('restores pure-reduction wire parameters without broadening intent', async () => {
    const storage = memory(), order = new FxPendingNetOrder(1, storage)
    const body = { side: 'buy' as const, amount: '50.000001', borrow_amount: '0',
      min_out: '40.000001', expected_economic_version: 7, allow_growth: false }
    await expect(order.start(7, body, async () => { throw new Error('lost') })).rejects.toThrow('lost')
    const saved = order.pending
    const restored = new FxPendingNetOrder(1, storage)
    const receipt = await restored.retry(async request => {
      expect(request).toEqual(saved)
      expect(request.body).toMatchObject(body)
      return { succeeded: true }
    })
    expect(receipt).toEqual({ succeeded: true })
    expect(new FxPendingNetOrder(1, storage).hasUnresolved).toBe(false)
  })
})

describe('manual review of a pending FX order', () => {
  const body = { side: 'buy' as const, amount: '50', borrow_amount: '0',
    min_out: '40', expected_economic_version: 7, allow_growth: false }

  it('clears a corrupt local record after review and permits a new order', async () => {
    const storage = memory()
    storage.setItem('fx-net-pending-v1:1', '{invalid')
    const order = new FxPendingNetOrder(1, storage)
    expect(order.hasUnresolved).toBe(true)
    expect(order.clearAfterReview()).toBe(true)
    expect(order.storageError).toBe(false)
    expect(new FxPendingNetOrder(1, storage).hasUnresolved).toBe(false)
    expect(await order.start(7, body, async () => 'new receipt')).toBe('new receipt')
  })

  it('keeps an unknown order blocked if manual removal fails', async () => {
    const storage = memory(), order = new FxPendingNetOrder(1, storage)
    await expect(order.start(7, body, async () => { throw new Error('lost') })).rejects.toThrow('lost')
    const original = order.pending
    storage.removeItem = () => { throw new Error('unavailable') }
    expect(order.clearAfterReview()).toBe(false)
    expect(order.pending).toEqual(original)
    expect(order.hasUnresolved).toBe(true)
    expect(order.storageError).toBe(true)
  })

  it('returns a successful receipt even when removing the saved identity fails', async () => {
    const storage = memory(), order = new FxPendingNetOrder(1, storage)
    const removeItem = storage.removeItem
    storage.removeItem = () => { throw new Error('unavailable') }
    expect(await order.start(7, body, async () => 'committed')).toBe('committed')
    expect(order.storageError).toBe(true)
    storage.removeItem = removeItem
    expect(order.clearAfterReview()).toBe(true)
    expect(order.hasUnresolved).toBe(false)
  })

  it('does not discard an order while its submission is in flight', async () => {
    const order = new FxPendingNetOrder(1, memory())
    let finish!: (value: string) => void
    const result = order.start(7, body, () => new Promise<string>(resolve => { finish = resolve }))
    expect(order.clearAfterReview()).toBe(false)
    expect(order.pending).not.toBeNull()
    finish('committed')
    expect(await result).toBe('committed')
  })
})
