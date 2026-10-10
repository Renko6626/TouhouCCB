<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, useDialog, useMessage } from 'naive-ui'
import { adminSystemApi, type BackendSystemStatus } from '@/api/admin'

const props = defineProps<{ hasUnsavedChanges: boolean; saving: boolean }>()
const emit = defineEmits<{ busy: [value: boolean] }>()
const dialog = useDialog()
const msg = useMessage()
const system = ref<BackendSystemStatus | null>(null)
const phase = ref<'idle' | 'requesting' | 'waiting' | 'success' | 'error'>('idle')
const feedback = ref('')
const checking = ref(false)
const busy = computed(() => phase.value === 'requesting' || phase.value === 'waiting')
const disabled = computed(() => busy.value || props.saving || props.hasUnsavedChanges
  || !system.value?.restart_enabled || system.value.restart_pending
  || system.value.cooldown_seconds > 0)
const hint = computed(() => {
  if (props.hasUnsavedChanges) return '请先保存或撤销当前修改。'
  if (!system.value) return '正在确认后端状态。'
  if (system.value.reason) return system.value.reason
  if (system.value.restart_pending) return '后端正在重启，请等待恢复。'
  if (system.value.cooldown_seconds > 0) return `还需等待 ${system.value.cooldown_seconds} 秒才能再次重启。`
  return '重启后加载已保存配置，交易会短暂不可用。'
})
watch(busy, value => emit('busy', value))

let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false
let previousInstance = ''
let deadline = 0
const controller = new AbortController()

function schedule(delay = 2000) {
  clearTimeout(timer)
  if (!disposed) timer = setTimeout(() => void refresh(), delay)
}

async function refresh() {
  if (disposed || checking.value) return
  clearTimeout(timer)
  checking.value = true
  try {
    const next = await adminSystemApi.status(controller.signal)
    if (disposed) return
    system.value = next
    if (phase.value === 'waiting' && next.instance_id !== previousInstance && !next.restart_pending) {
      phase.value = 'success'
      feedback.value = '后端已重启，服务已恢复。'
      msg.success(feedback.value)
    } else if (!busy.value && phase.value !== 'success') {
      feedback.value = ''
    }
  } catch (error: unknown) {
    if (disposed) return
    if (!busy.value) {
      system.value = null
      feedback.value = (error as { message?: string }).message ?? '无法读取后端状态，请稍后刷新。'
      phase.value = 'error'
    }
  } finally {
    checking.value = false
  }
  if (disposed) return
  if (phase.value === 'waiting') {
    if (Date.now() >= deadline) {
      phase.value = 'error'
      system.value = null
      feedback.value = '尚未确认后端恢复，请刷新状态或查看服务器日志。'
    } else schedule()
  } else if (system.value && (system.value.cooldown_seconds > 0 || system.value.restart_pending)) {
    schedule(3000)
  }
}

async function restart() {
  if (disposed || disabled.value || !system.value) return
  clearTimeout(timer)
  previousInstance = system.value.instance_id
  phase.value = 'requesting'
  feedback.value = '正在提交重启请求。'
  try {
    await adminSystemApi.restart(previousInstance, controller.signal)
  } catch (error: unknown) {
    if (disposed) return
    const status = (error as { status?: number }).status
    // Do not resend an ambiguous request; verify a new instance instead.
    if ((status && status < 500) || status === 503) {
      phase.value = 'error'
      feedback.value = (error as { message?: string }).message ?? '重启请求被拒绝。'
      msg.error(feedback.value)
      schedule()
      return
    }
  }
  if (disposed) return
  phase.value = 'waiting'
  feedback.value = '正在重启，等待后端恢复。'
  deadline = Date.now() + 90_000
  schedule()
}

function confirmRestart() {
  dialog.warning({
    title: '重启后端？',
    content: '交易会短暂不可用。后端将完成停机清理，重启后加载已保存配置。',
    positiveText: '确认重启',
    negativeText: '取消',
    onPositiveClick: restart,
  })
}

onMounted(() => void refresh())
onBeforeUnmount(() => {
  disposed = true
  clearTimeout(timer)
  controller.abort()
  emit('busy', false)
})
</script>

<template>
  <section class="maintenance-panel">
    <div>
      <h2>后端维护</h2>
      <p>{{ hint }}</p>
    </div>
    <div class="maintenance-actions">
      <NButton :loading="checking" :disabled="busy" @click="refresh">刷新状态</NButton>
      <NButton type="warning" :loading="busy" :disabled="disabled" @click="confirmRestart">
        {{ busy ? '正在重启' : '重启后端' }}
      </NButton>
    </div>
    <NAlert v-if="feedback" :type="phase === 'error' ? 'error' : phase === 'success' ? 'success' : 'info'">
      {{ feedback }}
    </NAlert>
  </section>
</template>

<style scoped>
.maintenance-panel {
  border: 2px solid #000;
  padding: 16px;
  margin-bottom: 16px;
  background: #fff;
}
h2 { margin: 0 0 8px; font-size: 18px; }
p { margin: 0; color: #666; font-size: 13px; }
.maintenance-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
</style>
