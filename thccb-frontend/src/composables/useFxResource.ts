import { ref, shallowRef } from 'vue'

/** FX 局部刷新：保留旧快照、显式报错，且禁止旧币种/旧请求回填。 */
export function useFxResource<T>() {
  const data = shallowRef<T | null>(null)
  const loading = ref(false)
  const failed = ref(false)
  const updatedAt = ref('')
  let generation = 0

  async function load(fetcher: () => Promise<T>) {
    const request = ++generation
    loading.value = true
    try {
      const result = await fetcher()
      if (request !== generation) return
      data.value = result
      failed.value = false
      updatedAt.value = new Date().toLocaleTimeString()
    } catch {
      if (request === generation) failed.value = true
    } finally {
      if (request === generation) loading.value = false
    }
  }

  function reset() {
    generation++
    data.value = null
    loading.value = false
    failed.value = false
    updatedAt.value = ''
  }

  return { data, loading, failed, updatedAt, load, reset }
}
