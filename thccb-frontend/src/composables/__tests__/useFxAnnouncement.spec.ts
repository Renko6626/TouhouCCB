import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'
import { fxAnnouncementApi } from '@/api/fxAnnouncement'
import { useFxAnnouncement } from '@/composables/useFxAnnouncement'

vi.mock('@/api/fxAnnouncement', () => ({ fxAnnouncementApi: { latest: vi.fn() } }))
const scopes: ReturnType<typeof effectScope>[] = []
const first = { id: 1, content: '决赛后公布注资结果。', created_at: '2026-10-16T08:00:00Z' }

function mount(user: () => number | undefined) {
  const scope = effectScope()
  scopes.push(scope)
  return scope.run(() => useFxAnnouncement(user))!
}

beforeEach(() => {
  const values = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
    removeItem: (key: string) => { values.delete(key) },
  })
  vi.mocked(fxAnnouncementApi.latest).mockResolvedValue(first)
})
afterEach(() => { scopes.splice(0).forEach(s => s.stop()); vi.unstubAllGlobals(); vi.clearAllMocks() })

it('关闭后重入仍隐藏，可重新查看，新公告重新显示', async () => {
  const notice = mount(() => 1)
  await notice.refresh()
  expect(notice.visible.value).toBe(true)
  notice.dismiss()
  expect(notice.visible.value).toBe(false)
  const reentered = mount(() => 1)
  await reentered.refresh()
  expect(reentered.visible.value).toBe(false)
  reentered.show()
  expect(reentered.visible.value).toBe(true)
  reentered.dismiss()
  vi.mocked(fxAnnouncementApi.latest).mockResolvedValue({ ...first, id: 2, content: '注资已完成。' })
  await reentered.refresh()
  expect(reentered.visible.value).toBe(true)
  expect(reentered.latest.value?.content).toBe('注资已完成。')
})

it('切换账号使用各自的本地关闭记录', async () => {
  const user = ref(1)
  const notice = mount(() => user.value)
  await notice.refresh()
  notice.dismiss()
  user.value = 2
  expect(notice.visible.value).toBe(true)
  user.value = 1
  expect(notice.visible.value).toBe(false)
})
