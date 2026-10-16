<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { fxAnnouncementApi, type FxAnnouncement } from '@/api/fxAnnouncement'
import { mapFxError } from '@/api/fx'
import { useFxResource } from '@/composables/useFxResource'

const msg = useMessage()
const content = ref('')
const publishing = ref(false)
const notice = useFxResource<FxAnnouncement | null>()
const { data: latest, failed, loading } = notice
const refresh = () => notice.load(fxAnnouncementApi.latest)

async function publish() {
  const text = content.value.trim()
  if (!text || publishing.value) return
  publishing.value = true
  try {
    const result = await fxAnnouncementApi.publish(text)
    // Invalidate a pending initial read before installing the committed notice.
    notice.reset()
    latest.value = result
    content.value = ''
    msg.success('公告已发布')
  } catch (e) {
    msg.error(mapFxError(e, '公告发布失败，请核对当前公告后再试'))
  } finally { publishing.value = false }
}
onMounted(refresh)
</script>

<template>
  <section class="fx-panel">
    <h2>交易公告</h2>
    <p class="fx-announcement-current" v-if="latest">当前公告：{{ latest.content }}</p>
    <p class="fx-hint" v-else-if="loading">公告读取中…</p>
    <p class="fx-hint" v-else-if="!failed">尚未发布公告。</p>
    <p v-if="failed" class="fx-error" role="alert">公告读取失败。<button type="button" class="fx-text-button" @click="refresh">重新读取</button></p>
    <form class="fx-announcement-form" @submit.prevent="publish">
      <input v-model="content" class="fx-input" aria-label="公告正文" placeholder="填写公告正文" maxlength="280" required :disabled="publishing" />
      <button class="btn-primary" type="submit" :disabled="publishing || !content.trim()">{{ publishing ? '发布中…' : '发布公告' }}</button>
    </form>
    <p class="fx-hint">最多 280 字符。发布后不能修改，交易页只展示最新一条；需要更正时请发布新公告。</p>
  </section>
</template>

<style scoped>
.fx-announcement-form { display: flex; gap: 10px; flex-wrap: wrap; }
.fx-announcement-form input { flex: 1; min-width: 180px; }
.fx-announcement-current { font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
</style>
