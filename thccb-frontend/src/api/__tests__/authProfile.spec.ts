import axios, { AxiosError, AxiosHeaders, type AxiosAdapter } from 'axios'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAuthStore } from '@/stores/auth'
import { authApi } from '../auth'
import type { User } from '@/types/api'
beforeEach(() => {
  vi.stubGlobal('localStorage', { getItem: vi.fn().mockReturnValue(null), setItem: vi.fn(), removeItem: vi.fn() })
  vi.stubGlobal('window', { location: { href: '', origin: 'http://test' }, dispatchEvent: vi.fn() })
  setActivePinia(createPinia())
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })
function login(id: number, access: string, refresh: string) {
  const auth = useAuthStore(); auth.user = { id } as User; auth.accessToken = access; auth.refreshToken = refresh
  return auth
}
for (const status of [401, 403]) it(`discards an old profile ${status} before renewal/logout/private dialogs in the new login`, async () => {
  const auth = login(1, 'A', 'RA')
  let reject!: () => void, started!: () => void
  const reached = new Promise<void>(resolve => { started = resolve })
  const adapter: AxiosAdapter = config => { started(); return new Promise((_, fail) => { reject = () => fail(new AxiosError('expired', 'ERR_BAD_REQUEST', config, undefined, { status, data: { detail: 'USER_BANNED' }, statusText: '', headers: new AxiosHeaders(), config })) }) }
  const actualProfile = authApi.getCurrentUser.bind(authApi)
  vi.spyOn(authApi, 'getCurrentUser').mockImplementation(() => actualProfile({ adapter }))
  const renew = vi.spyOn(axios, 'post')
  const logout = vi.spyOn(auth, 'logout')
  const result = auth.fetchCurrentUser(); await reached
  login(2, 'B', 'RB'); reject(); await result
  expect(auth.user!.id).toBe(2); expect(auth.accessToken).toBe('B')
  expect(renew).not.toHaveBeenCalled(); expect(logout).not.toHaveBeenCalled()
  expect(window.dispatchEvent).not.toHaveBeenCalled()
})
it('loads an initial user-null profile with same-session credential renewal', async () => {
  const auth = useAuthStore(); auth.accessToken = 'A'; auth.refreshToken = 'RA'
  expect(auth.user).toBeNull()
  const sent: string[] = []
  const adapter: AxiosAdapter = async config => {
    sent.push(String(config.headers.Authorization))
    if (sent.length === 1) throw new AxiosError('expired', 'ERR_BAD_REQUEST', config, undefined, { status: 401, data: {}, statusText: '', headers: new AxiosHeaders(), config })
    return { status: 200, data: { id: 1 }, statusText: 'OK', headers: new AxiosHeaders(), config }
  }
  const actualProfile = authApi.getCurrentUser.bind(authApi)
  vi.spyOn(authApi, 'getCurrentUser').mockImplementation(() => actualProfile({ adapter }))
  vi.spyOn(axios, 'post').mockResolvedValue({ data: { access_token: 'renewed-A' } })
  await auth.fetchCurrentUser()
  expect(auth.user!.id).toBe(1); expect(sent).toEqual(['Bearer A', 'Bearer renewed-A'])
})
