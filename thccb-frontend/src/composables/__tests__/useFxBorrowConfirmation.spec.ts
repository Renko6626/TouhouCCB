import { expect, it } from 'vitest'
import { useFxBorrowConfirmation } from '../useFxBorrowConfirmation'
const details = {
  kind: 'long' as const,
  currency: '摩拉',
  borrowAmount: '20',
  cashInput: '80',
  quantity: '50',
  lockedGold: null,
  equity: '100',
  basis: '200',
  maintenance: '0.2',
  reliable: true,
}
it('requires acceptance, submits the captured order once, and only saves opt-out on acceptance', async () => {
  let key = 'account:1:amount:100',
    enabled = true
  const trades: string[] = []
  const gate = useFxBorrowConfirmation(
    () => key,
    () => enabled,
    () => {
      enabled = false
    },
  )
  await gate.request(details, Date.now() + 30000, async () => {
    trades.push('original')
  })
  expect(trades).toEqual([])
  gate.cancel()
  expect(enabled).toBe(true)
  await gate.request(details, Date.now() + 30000, async () => {
    trades.push('original')
  })
  await gate.confirm(true)
  await gate.confirm(true)
  expect(trades).toEqual(['original'])
  expect(enabled).toBe(false)
  await gate.request(details, Date.now() + 30000, async () => {
    trades.push('next')
  })
  expect(trades).toEqual(['original', 'next'])
  key = 'account:2:amount:100'
})
it('rejects changed accounts/orders and expired confirmations without saving opt-out', async () => {
  let key = 'account:1:amount:100',
    submissions = 0,
    optOuts = 0
  const gate = useFxBorrowConfirmation(
    () => key,
    () => true,
    () => {
      optOuts++
    },
  )
  await gate.request(details, Date.now() + 30000, async () => {
    submissions++
  })
  key = 'account:2:amount:100'
  await gate.confirm(true)
  expect(submissions).toBe(0)
  expect(optOuts).toBe(0)
  expect(gate.error.value).toContain('订单已变化')
  await gate.request(details, Date.now() - 1, async () => {
    submissions++
  })
  await gate.confirm(true)
  expect(submissions).toBe(0)
  expect(optOuts).toBe(0)
  expect(gate.error.value).toContain('报价已过期')
})
