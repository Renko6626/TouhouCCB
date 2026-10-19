import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { effectScope, nextTick, reactive } from 'vue'
import type { WavelengthRound, WavelengthPrediction } from '@/api/wavelength'

const mocks = vi.hoisted(() => ({ current: vi.fn(), mine: vi.fn(), submit: vi.fn(), subscribe: vi.fn() }))
vi.mock('@/api/wavelength', () => ({ wavelengthApi: mocks }))
const auth = reactive({ user: { id: 1, cash: '500' }, sessionGeneration: 1, fetchCurrentUser: vi.fn() })
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))
import { useWavelength } from '../useWavelength'

const round: WavelengthRound = {
  round_key: 'g:1', game_key: 'g', round_number: 1, left_label: '寒冷', right_label: '炎热',
  team_name: '蓝队', status: 'open', stake: '100', bands: [{ max_distance: 16, payout: '300' }],
  target_position: null, created_at: '', closed_at: null, finished_at: null,
}
const empty: WavelengthPrediction = {
  round_key: 'g:1', status: 'open', position: null, stake: '0', distance: null,
  payout: null, refund: '0', net_profit: null, submitted_at: null,
}
let change: () => void
let connection: (value: boolean) => void
let scope: ReturnType<typeof effectScope>
let stop: ReturnType<typeof vi.fn>
beforeEach(() => {
  vi.clearAllMocks()
  auth.fetchCurrentUser.mockReset()
  auth.user = { id: 1, cash: '500' }; auth.sessionGeneration = 1
  mocks.current.mockResolvedValue({ ...round }); mocks.mine.mockResolvedValue({ ...empty })
  stop = vi.fn()
  mocks.subscribe.mockImplementation((onChange, onConnection) => {
    change = onChange; connection = onConnection; return stop
  })
  scope = effectScope()
})
afterEach(() => scope.stop())
async function create() {
  const page = scope.run(() => useWavelength())!
  await page.start()
  connection(true)
  return page
}

it('keeps the current round when a balance refresh replaces the same user profile', async () => {
  auth.fetchCurrentUser.mockImplementationOnce(async () => { auth.user = { ...auth.user, cash: '700' } })
  const page = await create()
  await nextTick()
  expect(page.round.value?.round_key).toBe('g:1')
  expect(mocks.current).toHaveBeenCalledTimes(1)
})

it('charges only on explicit submit and preserves an unsaved edit during live refresh', async () => {
  const page = await create()
  page.setPosition(250)
  expect(mocks.submit).not.toHaveBeenCalled()
  const accepted = { ...empty, position: 250, stake: '100' }
  mocks.submit.mockResolvedValue(accepted); mocks.mine.mockResolvedValue(accepted)
  await page.submit()
  expect(page.prediction.value?.position).toBe(250)
  expect(mocks.submit).toHaveBeenCalledExactlyOnceWith('g:1', 250)
  page.setPosition(300)
  await page.refresh()
  expect(page.position.value).toBe(300)
  expect(page.prediction.value?.position).toBe(250)
  expect(page.joined.value).toBe(true)
  expect(mocks.submit).toHaveBeenCalledTimes(1)
})

it('shows committed settlement after a round event without making another paid request', async () => {
  const page = await create()
  auth.fetchCurrentUser.mockClear()
  mocks.current.mockResolvedValue({ ...round, status: 'settled', target_position: 500 })
  mocks.mine.mockResolvedValue({ ...empty, status: 'settled', position: 510, stake: '100', distance: 10, payout: '300', net_profit: '200' })
  change()
  await vi.waitFor(() => expect(page.round.value?.status).toBe('settled'))
  expect(page.prediction.value?.payout).toBe('300')
  expect(page.canSubmit.value).toBe(false)
  expect(mocks.submit).not.toHaveBeenCalled()
  expect(auth.fetchCurrentUser).toHaveBeenCalled()
})

it('does not let an older response overwrite a sealed round, and blocks edits while disconnected', async () => {
  const page = await create()
  let resolve!: (value: WavelengthRound) => void
  mocks.current.mockReturnValueOnce(new Promise<WavelengthRound>(done => { resolve = done }))
  const old = page.refresh()
  mocks.current.mockResolvedValue({ ...round, status: 'closed' })
  mocks.mine.mockResolvedValue({ ...empty, status: 'closed' })
  await page.refresh()
  resolve({ ...round }); await old
  expect(page.round.value?.status).toBe('closed')
  connection(false)
  expect(page.canSubmit.value).toBe(false)
  await page.submit()
  expect(mocks.submit).not.toHaveBeenCalled()
  scope.stop(); await nextTick()
  expect(stop).toHaveBeenCalled()
})
