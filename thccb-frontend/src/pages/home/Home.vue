<script setup lang="ts">
import { defineAsyncComponent, onMounted, ref } from 'vue'
import api from '@/api'

defineOptions({ name: 'HomePage' })
const FxHome = defineAsyncComponent(() => import('./FxHome.vue'))
const PredictionHome = defineAsyncComponent(() => import('./PredictionHome.vue'))
const mode = ref<'fx' | 'prediction' | null>(null)
const failed = ref(false)
const loading = ref(false)

async function loadMode() {
  if (loading.value) return
  loading.value = true
  failed.value = false
  try {
    const result = await api.get<{ mode: 'fx' | 'prediction' }>('/api/v1/site/homepage')
    if (result.mode !== 'fx' && result.mode !== 'prediction') throw new Error('Invalid homepage mode')
    mode.value = result.mode
  } catch {
    failed.value = true
  } finally {
    loading.value = false
  }
}

onMounted(loadMode)
</script>

<template>
  <FxHome v-if="mode === 'fx'" />
  <PredictionHome v-else-if="mode === 'prediction'" />
  <div v-else class="home-state" role="status">
    <template v-if="failed">首页加载失败。<button @click="loadMode">重试</button></template>
    <template v-else>正在加载首页…</template>
  </div>
</template>

<style scoped>
.home-state { border: 2px solid #000; padding: 32px; text-align: center; }
button { margin-left: 12px; border: 2px solid #000; padding: 4px 12px; background: #fff; cursor: pointer; }
</style>
