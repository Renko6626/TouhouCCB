<script setup lang="ts">
import type { FxAnnouncement } from '@/api/fxAnnouncement'

defineProps<{ announcement: FxAnnouncement | null; visible: boolean; failed: boolean }>()
defineEmits<{ dismiss: []; show: []; retry: [] }>()
</script>

<template>
  <div v-if="announcement || failed" class="fx-announcement">
    <div v-if="announcement && visible" class="fx-announcement-banner" role="status" aria-live="polite">
      <span class="fx-announcement-content">{{ announcement.content }}</span>
      <button type="button" class="fx-announcement-close" aria-label="关闭公告" @click="$emit('dismiss')">×</button>
    </div>
    <div v-if="(announcement && !visible) || failed" class="fx-announcement-controls">
      <button v-if="announcement && !visible" type="button" @click="$emit('show')">查看公告</button>
      <template v-if="failed">
        <span>公告更新失败。</span><button type="button" @click="$emit('retry')">重试</button>
      </template>
    </div>
  </div>
</template>

<style scoped>
.fx-announcement { margin: 10px 0; }
.fx-announcement-banner {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 10px 6px 14px;
  border: 1px solid #c4a258;
  background: #fffbeb;
  color: #694713;
  font-size: 13px;
  line-height: 1.6;
}
.fx-announcement-content { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.fx-announcement-close {
  flex: none;
  width: 30px;
  height: 30px;
  border: 0;
  background: transparent;
  color: inherit;
  font-size: 22px;
  cursor: pointer;
}
.fx-announcement-close:hover { background: #f6eccd; }
.fx-announcement-controls { display: flex; gap: 8px; align-items: center; font-size: 12px; color: #666; }
.fx-announcement-controls button {
  padding: 2px 0;
  border: 0;
  background: transparent;
  color: inherit;
  text-decoration: underline;
  cursor: pointer;
}
</style>
