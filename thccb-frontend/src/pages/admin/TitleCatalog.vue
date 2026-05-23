<script setup lang="ts">
import { ref, onMounted, h } from 'vue'
import {
  useMessage, NButton, NDataTable, NModal, NForm, NFormItem,
  NInput, NInputNumber, NSwitch,
  type DataTableColumns,
} from 'naive-ui'
import { adminTitleApi } from '@/api/admin'
import type { TitleRead } from '@/api/title'
import TitleChip from '@/components/title/TitleChip.vue'

const message = useMessage()
const titles = ref<TitleRead[]>([])
const showEditor = ref(false)
const editing = ref<TitleRead | null>(null)

const formName = ref('')
const formDesc = ref('')
const formColor = ref('#000000')
const formIcon = ref('')
const formSort = ref<number>(100)
const formActive = ref(true)
const saving = ref(false)

async function refresh() {
  try {
    titles.value = await adminTitleApi.listTitles()
  } catch (e) {
    message.error((e as { message?: string })?.message || '加载失败')
  }
}

function openNew() {
  editing.value = null
  formName.value = ''
  formDesc.value = ''
  formColor.value = '#000000'
  formIcon.value = ''
  formSort.value = 100
  formActive.value = true
  showEditor.value = true
}

function openEdit(t: TitleRead) {
  editing.value = t
  formName.value = t.name
  formDesc.value = t.description
  formColor.value = t.color
  formIcon.value = t.icon
  formSort.value = t.sort_order
  formActive.value = t.is_active
  showEditor.value = true
}

async function save() {
  if (!formName.value.trim()) {
    message.error('名称不能为空')
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      await adminTitleApi.updateTitle(editing.value.id, {
        name: formName.value.trim(),
        description: formDesc.value,
        color: formColor.value,
        icon: formIcon.value,
        sort_order: formSort.value,
        is_active: formActive.value,
      } as Partial<TitleRead>)
    } else {
      await adminTitleApi.createTitle({
        name: formName.value.trim(),
        description: formDesc.value,
        color: formColor.value,
        icon: formIcon.value,
        sort_order: formSort.value,
      })
    }
    showEditor.value = false
    await refresh()
    message.success('保存成功')
  } catch (e) {
    message.error((e as { message?: string })?.message || '保存失败')
  } finally {
    saving.value = false
  }
}

const columns: DataTableColumns<TitleRead> = [
  { title: 'ID', key: 'id', width: 60 },
  {
    title: '预览',
    key: '_chip',
    width: 160,
    render(row) {
      return h(TitleChip, { title: row })
    },
  },
  { title: '名称', key: 'name' },
  { title: '说明', key: 'description' },
  { title: '排序', key: 'sort_order', width: 80 },
  {
    title: '启用',
    key: 'is_active',
    width: 80,
    render(row) {
      return row.is_active ? '✓' : '✗'
    },
  },
  {
    title: '操作',
    key: '_actions',
    width: 100,
    render(row) {
      return h(
        NButton,
        { size: 'small', onClick: () => openEdit(row) },
        () => '编辑'
      )
    },
  },
]

onMounted(refresh)
</script>

<template>
  <div class="p-6">
    <div class="flex justify-between items-center mb-4">
      <h1 class="text-2xl font-black border-b-4 border-black pb-1">称号目录</h1>
      <NButton type="primary" @click="openNew">+ 新建称号</NButton>
    </div>

    <NDataTable
      :columns="columns"
      :data="titles"
      :bordered="true"
      :row-key="(row: TitleRead) => row.id"
    />

    <NModal
      v-model:show="showEditor"
      preset="card"
      :title="editing ? '编辑称号' : '新建称号'"
      style="max-width:500px;"
    >
      <NForm>
        <NFormItem label="名称">
          <NInput v-model:value="formName" maxlength="32" placeholder="VIP / Beta 测试者 等" />
        </NFormItem>
        <NFormItem label="说明">
          <NInput
            v-model:value="formDesc"
            maxlength="200"
            type="textarea"
            placeholder="可选"
          />
        </NFormItem>
        <NFormItem label="颜色 (hex)">
          <NInput v-model:value="formColor" maxlength="16" placeholder="#FFD700" />
        </NFormItem>
        <NFormItem label="图标 (emoji 或 1 字符)">
          <NInput v-model:value="formIcon" maxlength="16" placeholder="★" />
        </NFormItem>
        <NFormItem label="排序 (小者优先)">
          <NInputNumber v-model:value="formSort" :min="0" />
        </NFormItem>
        <NFormItem v-if="editing" label="启用">
          <NSwitch v-model:value="formActive" />
        </NFormItem>
        <div class="flex justify-end gap-2">
          <NButton @click="showEditor = false">取消</NButton>
          <NButton type="primary" :loading="saving" @click="save">保存</NButton>
        </div>
      </NForm>
    </NModal>
  </div>
</template>
