import axios, { AxiosError, AxiosHeaders, type AxiosAdapter } from 'axios'
import { beforeEach, expect, it, vi } from 'vitest'
import api from '../index'

const auth = vi.hoisted(() => ({ user: { id: 1 }, accessToken: 'access-A', refreshToken: 'refresh-A', logout: vi.fn() }))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => auth }))
beforeEach(() => {
  vi.restoreAllMocks()
  auth.user = { id: 1 }; auth.accessToken = 'access-A'; auth.refreshToken = 'refresh-A'
  auth.logout.mockClear()
  vi.stubGlobal('localStorage', { setItem: vi.fn() })
})
function switchUser() { auth.user = { id: 2 }; auth.accessToken = 'access-B'; auth.refreshToken = 'refresh-B' }

it('never refreshes or resends a delayed 401 under a different user', async () => {
  let fail!: () => void
  let reached!: () => void
  const started = new Promise<void>(resolve => { reached = resolve })
  const sent: string[] = []
  const adapter: AxiosAdapter = config => {
    sent.push(String(config.headers.Authorization))
    reached()
    return new Promise((_, reject) => { fail = () => reject(new AxiosError('expired', 'ERR_BAD_REQUEST', config,
      undefined, { status: 401, data: {}, statusText: '', headers: new AxiosHeaders(), config })) })
  }
  const refresh = vi.spyOn(axios, 'post')
  const result = api.postForCurrentUser('/finance', { amount: '50' }, { adapter })
  await started
  switchUser()
  fail()
  await expect(result).rejects.toMatchObject({ code: 'AUTH_CONTEXT_CHANGED' })
  expect(sent).toEqual(['Bearer access-A'])
  expect(refresh).not.toHaveBeenCalled()
  expect(auth.accessToken).toBe('access-B')
})

it('discards a refresh result after user switching without overwriting new credentials', async () => {
  let finish!: (value: { data: { access_token: string } }) => void
  let reached!: () => void
  const started = new Promise<void>(resolve => { reached = resolve })
  vi.spyOn(axios, 'post').mockImplementation(() => {
    reached()
    return new Promise(resolve => { finish = resolve })
  })
  const sent: string[] = []
  const adapter: AxiosAdapter = async config => {
    sent.push(String(config.headers.Authorization))
    throw new AxiosError('expired', 'ERR_BAD_REQUEST', config, undefined,
      { status: 401, data: {}, statusText: '', headers: new AxiosHeaders(), config })
  }
  const result = api.postForCurrentUser('/finance', {}, { adapter })
  await started
  switchUser()
  finish({ data: { access_token: 'renewed-A' } })
  await expect(result).rejects.toMatchObject({ code: 'AUTH_CONTEXT_CHANGED' })
  expect(sent).toEqual(['Bearer access-A'])
  expect(auth.accessToken).toBe('access-B')
  expect(auth.logout).not.toHaveBeenCalled()
})

it('still renews and retries with the original identity when the account is unchanged', async () => {
  vi.spyOn(axios, 'post').mockResolvedValue({ data: { access_token: 'renewed-A' } })
  const sent: string[] = []
  const adapter: AxiosAdapter = async config => {
    sent.push(String(config.headers.Authorization))
    if (sent.length === 1) throw new AxiosError('expired', 'ERR_BAD_REQUEST', config, undefined,
      { status: 401, data: {}, statusText: '', headers: new AxiosHeaders(), config })
    return { status: 200, data: { trade_id: 7 }, statusText: 'OK', headers: new AxiosHeaders(), config }
  }
  await expect(api.postForCurrentUser('/finance', {}, { adapter })).resolves.toEqual({ trade_id: 7 })
  expect(sent).toEqual(['Bearer access-A', 'Bearer renewed-A'])
})

it('an older shared refresh cannot supply credentials to a new-account financing request', async () => {
  let finish!: (value: { data: { access_token: string } }) => void
  let reached!: () => void
  const started = new Promise<void>(resolve => { reached = resolve })
  vi.spyOn(axios, 'post').mockImplementation(() => {
    reached()
    return new Promise(resolve => { finish = resolve })
  })
  const adapter: AxiosAdapter = async config => {
    throw new AxiosError('expired', 'ERR_BAD_REQUEST', config, undefined,
      { status: 401, data: {}, statusText: '', headers: new AxiosHeaders(), config })
  }
  const old = api.post('/summary', {}, { adapter })
  await started
  switchUser()
  const finance = api.postForCurrentUser('/finance', {}, { adapter })
  // Reject before joining the previous account's refresh; this assertion
  // also verifies we do not leave the financing request awaiting its token.
  await expect(finance).rejects.toMatchObject({ code: 'AUTH_CONTEXT_CHANGED' })
  finish({ data: { access_token: 'renewed-A' } })
  await expect(old).rejects.toMatchObject({ code: 'AUTH_CONTEXT_CHANGED' })
  expect(auth.accessToken).toBe('access-B')
  expect(auth.logout).not.toHaveBeenCalled()
})
