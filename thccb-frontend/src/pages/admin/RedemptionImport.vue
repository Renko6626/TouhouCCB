<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { redemptionAdminApi } from '@/api/redemption'
import type { CsvImportPreview, CsvImportResult } from '@/types/redemption'

const route = useRoute()
const router = useRouter()
const batchId = Number(route.params.id)

const csvText = ref('')
const preview = ref<CsvImportPreview | null>(null)
const result = ref<CsvImportResult | null>(null)
const loading = ref(false)
const error = ref('')

async function onFile(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  csvText.value = await file.text()
}

async function doPreview() {
  loading.value = true
  error.value = ''
  preview.value = null
  result.value = null
  try {
    preview.value = await redemptionAdminApi.importPreview(batchId, csvText.value)
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || '预检失败'
  } finally {
    loading.value = false
  }
}

async function doCommit() {
  if (!preview.value) return
  const ok = confirm(
    `将写入 ${preview.value.new_codes.length} 个新码，` +
    `跳过 ${preview.value.duplicate_codes.length} 个重复 + ` +
    `${preview.value.invalid_codes.length} 个非法。继续？`,
  )
  if (!ok) return
  loading.value = true
  try {
    result.value = await redemptionAdminApi.importCommit(batchId, csvText.value)
    preview.value = null
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || '导入失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="page">
    <button class="back" @click="router.back()">← 返回</button>
    <h1 class="page-title">导入兑换码 (批次 #{{ batchId }})</h1>

    <section class="card">
      <h2>① 选择来源</h2>
      <p class="hint">
        CSV 单列；首行 <code>code</code> 视为表头自动跳过；空行/前后空白自动 trim。
      </p>
      <input type="file" accept=".csv,.txt" @change="onFile" />
      <p>或直接粘贴：</p>
      <textarea v-model="csvText" rows="10" placeholder="ABC123&#10;DEF456&#10;..."></textarea>
    </section>

    <section class="card">
      <h2>② 预检</h2>
      <button
        class="btn-primary"
        :disabled="!csvText.trim() || loading"
        @click="doPreview"
      >
        预检
      </button>

      <div v-if="preview" class="preview">
        <p>共解析 {{ preview.total_lines }} 条：</p>
        <ul>
          <li>✅ 即将新增 <b>{{ preview.new_codes.length }}</b> 条</li>
          <li>⚠️ 与已有码重复（跳过）<b>{{ preview.duplicate_codes.length }}</b> 条</li>
          <li>❌ 非法（空/超长，跳过）<b>{{ preview.invalid_codes.length }}</b> 条</li>
        </ul>
        <details v-if="preview.duplicate_codes.length">
          <summary>查看重复码</summary>
          <pre>{{ preview.duplicate_codes.join('\n') }}</pre>
        </details>
        <details v-if="preview.invalid_codes.length">
          <summary>查看非法码</summary>
          <pre>{{ preview.invalid_codes.join('\n') }}</pre>
        </details>
        <button
          class="btn-primary"
          :disabled="loading || preview.new_codes.length === 0"
          @click="doCommit"
        >
          ③ 确认写入
        </button>
      </div>
    </section>

    <section v-if="result" class="card success">
      <h2>导入完成</h2>
      <p>
        成功写入 <b>{{ result.inserted }}</b> 条；
        跳过重复 {{ result.skipped_duplicate }} / 非法 {{ result.skipped_invalid }}。
      </p>
      <button class="btn-secondary" @click="router.push('/admin/redemption/batches')">
        返回批次列表
      </button>
    </section>

    <p v-if="error" class="error">{{ error }}</p>
  </div>
</template>

<style scoped>
.page { padding: 16px; max-width: 900px; margin: 0 auto; }
.page-title { font-size: 22px; font-weight: 700; margin-bottom: 16px; }
.back { background: none; border: none; cursor: pointer; padding: 8px 0; font-family: inherit; }
.card {
  border: 2px solid #000; padding: 16px; margin-bottom: 16px; background: #fff;
  box-shadow: 4px 4px 0 #000;
}
.card h2 { font-size: 16px; font-weight: 700; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.04em; }
.hint { color: #666; font-size: 12px; margin: 8px 0; }
.success { border-color: #16a34a; background: #f0fdf4; box-shadow: 4px 4px 0 #16a34a; }
textarea {
  width: 100%; padding: 8px; border: 1px solid #000;
  font-family: monospace;
}
.preview {
  margin-top: 16px; padding: 12px; background: #f5f5f5; border: 1px dashed #999;
}
.preview ul { margin: 8px 0 12px 20px; font-variant-numeric: tabular-nums; }
.preview pre {
  background: #fff; padding: 8px; max-height: 240px; overflow: auto; font-size: 12px;
  word-break: break-all; white-space: pre-wrap;
}
.btn-primary, .btn-secondary {
  border: 2px solid #000; padding: 8px 20px; cursor: pointer;
  font-weight: 600; font-family: inherit;
  transition: transform 0.1s, box-shadow 0.1s;
}
.btn-primary { background: #000; color: #fff; box-shadow: 4px 4px 0 #444; }
.btn-primary:hover:not(:disabled) { transform: translate(-1px, -1px); box-shadow: 5px 5px 0 #444; }
.btn-primary:disabled { opacity: 0.4; cursor: not-allowed; box-shadow: none; }
.btn-secondary { background: #fff; color: #000; box-shadow: 2px 2px 0 #000; }
.btn-secondary:hover { transform: translate(-1px, -1px); box-shadow: 3px 3px 0 #000; }
.error { color: #dc2626; margin-top: 12px; }
</style>
