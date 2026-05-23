<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useMessage, NCard, NInput, NButton, NEmpty } from 'naive-ui'
import { titleApi, type MyTitleItem } from '@/api/title'
import TitleChip from '@/components/title/TitleChip.vue'

const msg = useMessage()
const code = ref('')
const loading = ref(false)
const myTitles = ref<MyTitleItem[]>([])

async function refresh() {
  try {
    const data = await titleApi.myTitles()
    myTitles.value = data.titles
  } catch (e) {
    msg.error((e as { message?: string })?.message || '加载称号失败')
  }
}

async function redeem() {
  if (!code.value.trim() || loading.value) return
  loading.value = true
  try {
    const r = await titleApi.redeem(code.value.trim())
    msg.success(`获得新称号：${r.title.name}，可在个人页佩戴`)
    code.value = ''
    await refresh()
  } catch (e) {
    msg.error((e as { message?: string })?.message || '兑换失败')
  } finally {
    loading.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <div class="p-6 max-w-3xl mx-auto">
    <h1 class="text-2xl font-black mb-6 border-b-4 border-black pb-2">称号兑换</h1>

    <NCard
      class="mb-6"
      :bordered="false"
      style="box-shadow:6px 6px 0 #000; border:2px solid #000;"
    >
      <div class="flex gap-2 items-center">
        <NInput
          v-model:value="code"
          placeholder="输入激活码 (4-64 位)"
          maxlength="64"
          @keyup.enter="redeem"
        />
        <NButton
          type="primary"
          :loading="loading"
          @click="redeem"
          :disabled="!code.trim()"
        >
          兑换
        </NButton>
      </div>
      <p class="text-xs text-gray-600 mt-2">
        激活码区分大小写，每个码仅可使用一次。来源由管理员发放。
      </p>
    </NCard>

    <div>
      <h2 class="text-lg font-bold mb-2">我的称号</h2>
      <NEmpty v-if="!myTitles.length" description="还没有任何称号" />
      <div v-else class="flex flex-wrap gap-2">
        <TitleChip
          v-for="item in myTitles"
          :key="item.title.id"
          :title="item.title"
        />
      </div>
    </div>
  </div>
</template>
