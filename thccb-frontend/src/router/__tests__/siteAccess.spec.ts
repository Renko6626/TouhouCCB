import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { RouteLocationNormalized, NavigationGuardNext } from 'vue-router'
import type { User } from '@/types/api'
import { globalBeforeEach } from '../guards'
import { useAuthStore } from '@/stores/auth'
import { siteAccessApi } from '@/api/siteAccess'
import { authApi } from '@/api/auth'

vi.mock('@/api/casdoor', () => ({ getLoginUrl: async () => 'https://auth.test/login' }))

beforeEach(() => {
  vi.stubGlobal('localStorage', {
    getItem: vi.fn().mockReturnValue(null),
    setItem: vi.fn(),
    removeItem: vi.fn(),
  })
  vi.stubGlobal('window', { location: { href: '' }, dispatchEvent: vi.fn() })
  setActivePinia(createPinia())
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function route(name = 'home') {
  return { name, matched: [{ meta: {} }], query: {} } as unknown as RouteLocationNormalized
}
async function navigate(name = 'home') {
  const next = vi.fn()
  await globalBeforeEach(route(name), route(), next as NavigationGuardNext)
  return next
}

it('redirects a visitor before a disabled business page mounts', async () => {
  vi.spyOn(siteAccessApi, 'status').mockResolvedValue({ disabled: true })
  expect(await navigate()).toHaveBeenCalledWith('/access-denied')
})

it('uses the server profile instead of the cached administrator flag', async () => {
  const auth = useAuthStore()
  auth.accessToken = 'access'
  auth.user = { id: 1, is_superuser: true } as User
  vi.spyOn(siteAccessApi, 'status').mockResolvedValue({ disabled: true })
  vi.spyOn(authApi, 'getCurrentUser').mockResolvedValue({ id: 1, is_superuser: false } as User)
  expect(await navigate()).toHaveBeenCalledWith('/access-denied')
})

it('admits a validated administrator while the site is closed', async () => {
  const auth = useAuthStore()
  auth.accessToken = 'access'
  vi.spyOn(siteAccessApi, 'status').mockResolvedValue({ disabled: true })
  vi.spyOn(authApi, 'getCurrentUser').mockResolvedValue({ id: 1, is_superuser: true } as User)
  expect(await navigate()).toHaveBeenCalledWith()
})

it('keeps the authentication callback reachable without rechecking the gate', async () => {
  const status = vi.spyOn(siteAccessApi, 'status')
  expect(await navigate('auth-callback')).toHaveBeenCalledWith()
  expect(status).not.toHaveBeenCalled()
})
