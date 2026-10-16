import { computed, ref, watch } from 'vue'
import { fxAnnouncementApi, type FxAnnouncement } from '@/api/fxAnnouncement'
import { useFxResource } from './useFxResource'

export function useFxAnnouncement(userId: () => number | undefined) {
  const resource = useFxResource<FxAnnouncement | null>()
  const latest = resource.data
  const dismissedId = ref('')
  const storageKey = () => `fx-announcement:${userId() ?? 'guest'}:dismissed`
  watch(userId, () => {
    try { dismissedId.value = localStorage.getItem(storageKey()) ?? '' }
    catch { dismissedId.value = '' }
  }, { immediate: true, flush: 'sync' })

  const visible = computed(() => latest.value !== null && dismissedId.value !== String(latest.value.id))
  function dismiss() {
    if (!latest.value) return
    dismissedId.value = String(latest.value.id)
    try { localStorage.setItem(storageKey(), dismissedId.value) }
    catch { /* Closing still works for this visit when local storage is unavailable. */ }
  }
  function show() {
    dismissedId.value = ''
    try { localStorage.removeItem(storageKey()) }
    catch { /* Keep the announcement visible for this visit. */ }
  }

  return { latest, visible, failed: resource.failed, dismiss, show,
    refresh: () => resource.load(fxAnnouncementApi.latest), reset: resource.reset }
}
