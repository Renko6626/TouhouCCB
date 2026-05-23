import { defineStore } from 'pinia'
import { ref } from 'vue'
import { titleApi, type TitleRead } from '@/api/title'

export const useTitleStore = defineStore('title', () => {
  const catalog = ref<TitleRead[]>([])
  const loaded = ref(false)

  async function ensureLoaded() {
    if (loaded.value) return
    try {
      catalog.value = await titleApi.publicCatalog()
      loaded.value = true
    } catch {
      // 加载失败保持 unloaded，下次再试
    }
  }

  function reset() {
    catalog.value = []
    loaded.value = false
  }

  return { catalog, loaded, ensureLoaded, reset }
})
