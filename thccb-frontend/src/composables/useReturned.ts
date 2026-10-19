import { computed, onScopeDispose, ref, watch } from 'vue'
import { returnedApi, type ReturnedRound, type ReturnedPrediction } from '@/api/returned'
import { useAuthStore } from '@/stores/auth'

/** Page state only; submissions are explicit and never replayed by this page. */
export function useReturned() {
  const auth = useAuthStore()
  const round = ref<ReturnedRound | null>(null)
  const prediction = ref<ReturnedPrediction | null>(null)
  const position = ref(500)
  const loading = ref(false)
  const submitting = ref(false)
  const connected = ref(false)
  const disabled = ref(false)
  const error = ref('')
  const feedback = ref('')
  const ready = ref(false)
  let request = 0
  let edited = false
  let disposed = false
  let started = false
  let closeStream: (() => void) | undefined
  const joined = computed(() => prediction.value?.position != null)
  const changed = computed(() => !joined.value || position.value !== prediction.value?.position)
  const canSubmit = computed(() => round.value?.status === 'open' && ready.value && connected.value
    && !submitting.value && prediction.value != null && changed.value
    && (joined.value || Number(auth.user?.cash ?? 0) >= Number(round.value.stake)))

  async function refresh() {
    const ticket = ++request
    loading.value = true
    ready.value = false
    error.value = ''
    try {
      const current = await returnedApi.current()
      const mine = current ? await returnedApi.mine(current.round_key) : null
      if (disposed || ticket !== request) return
      const newRound = current?.round_key !== round.value?.round_key
      if (newRound || !edited) {
        position.value = mine?.position ?? 500
        edited = false
      }
      if (newRound) feedback.value = ''
      round.value = current && mine ? { ...current, status: mine.status } : current
      prediction.value = mine
      disabled.value = false
      ready.value = true
      void auth.fetchCurrentUser()
    } catch (e) {
      if (disposed || ticket !== request) return
      if ((e as { status?: number }).status === 404) {
        disabled.value = true
        round.value = null
        prediction.value = null
        closeStream?.(); closeStream = undefined; connected.value = false
      } else {
        error.value = (e as Error).message || '无法同步竞猜，请刷新后重试'
      }
    } finally {
      if (!disposed && ticket === request) loading.value = false
    }
  }

  function setPosition(value: number) {
    position.value = Math.max(0, Math.min(1000, Math.round(value)))
    edited = true
    feedback.value = ''
  }

  async function submit() {
    if (!canSubmit.value || !round.value) return
    const key = round.value.round_key, value = position.value, generation = auth.sessionGeneration
    const wasJoined = joined.value
    submitting.value = true
    error.value = ''
    try {
      const accepted = await returnedApi.submit(key, value)
      if (disposed || generation !== auth.sessionGeneration || round.value?.round_key !== key) return
      prediction.value = accepted
      edited = false
      feedback.value = wasJoined ? '竞猜位置已更新，没有再次扣款。' : '已参与本轮竞猜，已投入 100 金圆券。'
      await Promise.all([refresh(), auth.fetchCurrentUser()])
    } catch (e) {
      if (disposed || generation !== auth.sessionGeneration) return
      await refresh()
      if (disposed || generation !== auth.sessionGeneration) return
      error.value = (e as { status?: number }).status
        ? (e as Error).message
        : '提交结果未确认，请查看已保存的位置，或刷新后核对。'
    } finally {
      if (!disposed && generation === auth.sessionGeneration) submitting.value = false
    }
  }

  async function start() {
    if (disposed) return
    started = true
    closeStream?.()
    connected.value = false
    closeStream = returnedApi.subscribe(() => { void refresh() }, value => { connected.value = value })
    await refresh()
  }

  watch([() => auth.user?.id, () => auth.sessionGeneration], () => {
    ++request
    round.value = null; prediction.value = null; position.value = 500
    edited = false; submitting.value = false; feedback.value = ''; error.value = ''
    if (started) void refresh()
  })
  onScopeDispose(() => { disposed = true; ++request; closeStream?.() })

  return { round, prediction, position, loading, submitting, connected, disabled, error, feedback,
    joined, changed, canSubmit, setPosition, submit, refresh, start }
}
