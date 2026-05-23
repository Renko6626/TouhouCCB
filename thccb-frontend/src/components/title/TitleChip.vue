<script setup lang="ts">
import { computed } from 'vue'

interface TitleData {
  id?: number
  name: string
  color: string
  icon: string
}

const props = defineProps<{
  title: TitleData | null | undefined
  size?: 'sm' | 'md'
}>()

function pickTextColor(bg: string): string {
  const m = bg.replace('#', '')
  if (m.length !== 6) return '#000'
  const r = parseInt(m.slice(0, 2), 16)
  const g = parseInt(m.slice(2, 4), 16)
  const b = parseInt(m.slice(4, 6), 16)
  const luma = (0.299 * r + 0.587 * g + 0.114 * b) / 255
  return luma > 0.5 ? '#000' : '#fff'
}

const styleObj = computed(() => {
  if (!props.title) return {}
  return {
    backgroundColor: props.title.color || '#000',
    color: pickTextColor(props.title.color || '#000'),
  }
})

const sizeClass = computed(() =>
  props.size === 'sm' ? 'px-1.5 py-0 text-xs' : 'px-2 py-0.5 text-sm'
)
</script>

<template>
  <span
    v-if="title"
    :class="['inline-flex items-center gap-1 border-2 border-black font-bold', sizeClass]"
    :style="styleObj"
  >
    <span v-if="title.icon">{{ title.icon }}</span>
    <span>{{ title.name }}</span>
  </span>
</template>
