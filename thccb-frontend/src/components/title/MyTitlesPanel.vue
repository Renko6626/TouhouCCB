<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useMessage, NCard, NButton, NEmpty } from 'naive-ui'
import { titleApi, type MyTitleItem } from '@/api/title'
import TitleChip from './TitleChip.vue'

const msg = useMessage()
const myTitles = ref<MyTitleItem[]>([])
const equippedId = ref<number | null>(null)
const loading = ref(false)

const isEquipped = (tid: number) => equippedId.value === tid

async function refresh() {
  try {
    const data = await titleApi.myTitles()
    myTitles.value = data.titles
    equippedId.value = data.equipped_title_id
  } catch (e) {
    msg.error((e as { message?: string })?.message || '加载称号失败')
  }
}

async function toggleEquip(tid: number) {
  if (loading.value) return
  loading.value = true
  try {
    const target = isEquipped(tid) ? null : tid
    await titleApi.equip(target)
    equippedId.value = target
    msg.success(target === null ? '已取下称号' : '已佩戴称号')
  } catch (e) {
    msg.error((e as { message?: string })?.message || '操作失败')
  } finally {
    loading.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <NCard
    title="我的称号"
    :bordered="false"
    style="box-shadow:6px 6px 0 #000; border:2px solid #000;"
  >
    <NEmpty v-if="!myTitles.length" description="尚未拥有称号" />
    <div v-else class="space-y-2">
      <div
        v-for="item in myTitles"
        :key="item.title.id"
        class="flex items-center justify-between border-2 border-black p-2"
      >
        <div class="flex items-center gap-3">
          <TitleChip :title="item.title" />
          <span class="text-xs text-gray-600">{{ item.title.description }}</span>
        </div>
        <NButton
          size="small"
          :type="isEquipped(item.title.id) ? 'warning' : 'primary'"
          :loading="loading"
          @click="toggleEquip(item.title.id)"
        >
          {{ isEquipped(item.title.id) ? '取下' : '佩戴' }}
        </NButton>
      </div>
    </div>
  </NCard>
</template>
