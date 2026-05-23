<script setup lang="ts">
import { ref, onMounted, h, computed } from 'vue'
import {
  useMessage, NButton, NDataTable, NModal, NForm, NFormItem,
  NInput, NSelect, NUpload,
  type DataTableColumns,
  type UploadFileInfo,
} from 'naive-ui'
import { adminTitleApi } from '@/api/admin'
import type { TitleRead } from '@/api/title'

interface BatchRow {
  id: number
  title_id: number
  title_name: string
  name: string
  description: string
  total: number
  used: number
  created_at: string
}

const message = useMessage()
const batches = ref<BatchRow[]>([])
const titles = ref<TitleRead[]>([])

const showNew = ref(false)
const showImport = ref(false)
const newTitleId = ref<number | null>(null)
const newName = ref('')
const newDesc = ref('')
const importBatchId = ref<number | null>(null)
const importFile = ref<File | null>(null)
const loading = ref(false)

async function refresh() {
  try {
    batches.value = await adminTitleApi.listBatches()
    titles.value = await adminTitleApi.listTitles()
  } catch (e) {
    message.error((e as { message?: string })?.message || '加载失败')
  }
}

const titleOptions = computed(() =>
  titles.value
    .filter((t) => t.is_active)
    .map((t) => ({ label: t.name, value: t.id }))
)

function openNew() {
  newTitleId.value = null
  newName.value = ''
  newDesc.value = ''
  showNew.value = true
}

async function createBatch() {
  if (!newTitleId.value || !newName.value.trim()) {
    message.error('请填 title 和 name')
    return
  }
  loading.value = true
  try {
    await adminTitleApi.createBatch({
      title_id: newTitleId.value,
      name: newName.value.trim(),
      description: newDesc.value,
    })
    showNew.value = false
    await refresh()
    message.success('批次已创建')
  } catch (e) {
    message.error((e as { message?: string })?.message || '创建失败')
  } finally {
    loading.value = false
  }
}

function openImport(bid: number) {
  importBatchId.value = bid
  importFile.value = null
  showImport.value = true
}

function onFileChange(opts: { fileList: UploadFileInfo[] }) {
  const f = opts.fileList[0]
  importFile.value = (f?.file as File) ?? null
}

async function doImport() {
  if (!importBatchId.value || !importFile.value) {
    message.error('请选择 CSV 文件')
    return
  }
  loading.value = true
  try {
    const r = await adminTitleApi.importCodes(importBatchId.value, importFile.value)
    message.success(`成功导入 ${r.inserted} 个 code`)
    showImport.value = false
    await refresh()
  } catch (e) {
    message.error((e as { message?: string })?.message || '导入失败')
  } finally {
    loading.value = false
  }
}

const columns: DataTableColumns<BatchRow> = [
  { title: 'ID', key: 'id', width: 60 },
  { title: '称号', key: 'title_name' },
  { title: '批次名', key: 'name' },
  {
    title: '已用 / 总数',
    key: '_count',
    render(row) {
      return `${row.used} / ${row.total}`
    },
  },
  { title: '创建于', key: 'created_at', width: 180 },
  {
    title: '操作',
    key: '_actions',
    width: 120,
    render(row) {
      return h(
        NButton,
        { size: 'small', onClick: () => openImport(row.id) },
        () => '导入 CSV'
      )
    },
  },
]

onMounted(refresh)
</script>

<template>
  <div class="p-6">
    <div class="flex justify-between items-center mb-4">
      <h1 class="text-2xl font-black border-b-4 border-black pb-1">称号激活码批次</h1>
      <NButton type="primary" @click="openNew">+ 新建批次</NButton>
    </div>

    <NDataTable
      :columns="columns"
      :data="batches"
      :bordered="true"
      :row-key="(row: BatchRow) => row.id"
    />

    <NModal v-model:show="showNew" preset="card" title="新建批次" style="max-width:500px;">
      <NForm>
        <NFormItem label="称号">
          <NSelect
            v-model:value="newTitleId"
            :options="titleOptions"
            placeholder="选择称号"
          />
        </NFormItem>
        <NFormItem label="批次名">
          <NInput
            v-model:value="newName"
            maxlength="64"
            placeholder="2026-Q2 公测批次"
          />
        </NFormItem>
        <NFormItem label="说明（可选）">
          <NInput
            v-model:value="newDesc"
            maxlength="200"
            type="textarea"
          />
        </NFormItem>
        <div class="flex justify-end gap-2">
          <NButton @click="showNew = false">取消</NButton>
          <NButton type="primary" :loading="loading" @click="createBatch">
            创建
          </NButton>
        </div>
      </NForm>
    </NModal>

    <NModal
      v-model:show="showImport"
      preset="card"
      title="导入激活码 CSV"
      style="max-width:500px;"
    >
      <p class="mb-3 text-sm">
        CSV 格式：每行一个 code，可带表头 `code`。字符限 A-Z a-z 0-9 _ -
        长度 4-64，单批最多 5000 行。
      </p>
      <NUpload
        :max="1"
        accept=".csv"
        :default-upload="false"
        @change="onFileChange"
      >
        <NButton>选择 CSV</NButton>
      </NUpload>
      <div class="flex justify-end gap-2 mt-4">
        <NButton @click="showImport = false">取消</NButton>
        <NButton type="primary" :loading="loading" @click="doImport">
          导入
        </NButton>
      </div>
    </NModal>
  </div>
</template>
