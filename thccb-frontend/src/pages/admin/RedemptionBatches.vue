<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { redemptionAdminApi } from '@/api/redemption'
import type { BatchAdminItem, PartnerAdminItem, BatchStatus } from '@/types/redemption'

interface BatchForm {
  id?: number
  partner_id: number | ''
  name: string
  description: string
  unit_price: string
  status: BatchStatus
}

const router = useRouter()
const items = ref<BatchAdminItem[]>([])
const partners = ref<PartnerAdminItem[]>([])
const editing = ref<BatchForm | null>(null)

async function load() {
  items.value = await redemptionAdminApi.listBatches()
  partners.value = await redemptionAdminApi.listPartners()
}

function startCreate() {
  editing.value = {
    partner_id: partners.value[0]?.id ?? '',
    name: '', description: '', unit_price: '',
    status: 'draft',
  }
}

function startEdit(b: BatchAdminItem) {
  editing.value = {
    id: b.id, partner_id: b.partner_id, name: b.name,
    description: b.description, unit_price: b.unit_price, status: b.status,
  }
}

async function save() {
  if (!editing.value) return
  const f = editing.value
  if (f.id !== undefined) {
    const payload: Record<string, unknown> = {
      name: f.name, description: f.description, status: f.status,
    }
    if (f.status !== 'active') payload.unit_price = f.unit_price
    await redemptionAdminApi.updateBatch(f.id, payload)
  } else {
    if (typeof f.partner_id !== 'number') {
      alert('请选择合作方')
      return
    }
    await redemptionAdminApi.createBatch({
      partner_id: f.partner_id,
      name: f.name,
      description: f.description,
      unit_price: f.unit_price,
    })
  }
  editing.value = null
  await load()
}

async function changeStatus(b: BatchAdminItem, status: string) {
  await redemptionAdminApi.updateBatch(b.id, { status: status as BatchStatus })
  await load()
}

function goImport(b: BatchAdminItem) {
  router.push(`/admin/redemption/batches/${b.id}/import`)
}

onMounted(load)
</script>

<template>
  <div class="page">
    <h1 class="page-title">批次管理</h1>
    <button class="btn-primary" @click="startCreate" :disabled="partners.length === 0">
      + 新建批次
    </button>
    <p v-if="partners.length === 0" class="hint">先去「合作方管理」创建至少一个合作方。</p>

    <div class="table-wrap">
    <table class="table">
      <thead>
        <tr>
          <th>ID</th><th>合作方</th><th>名称</th><th>价格</th>
          <th>状态</th><th>库存</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="b in items" :key="b.id">
          <td>{{ b.id }}</td>
          <td>{{ b.partner_name }}</td>
          <td>{{ b.name }}</td>
          <td>{{ b.unit_price }}</td>
          <td>
            <select
              :value="b.status"
              @change="changeStatus(b, ($event.target as HTMLSelectElement).value)"
            >
              <option value="draft">草稿</option>
              <option value="active">上架</option>
              <option value="archived">下架</option>
            </select>
          </td>
          <td>{{ b.available_count }} / {{ b.total_count }}</td>
          <td>
            <button class="btn-sm" @click="startEdit(b)">编辑</button>
            <button class="btn-sm" @click="goImport(b)">导入码</button>
          </td>
        </tr>
      </tbody>
    </table>
    </div>

    <div v-if="editing" class="modal-bg" @click.self="editing = null">
      <div class="modal">
        <h3>{{ editing.id !== undefined ? '编辑' : '新建' }}批次</h3>
        <label v-if="editing.id === undefined">
          合作方
          <select v-model="editing.partner_id">
            <option v-for="p in partners" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
        </label>
        <label>批次名 <input v-model="editing.name" /></label>
        <label>
          描述（markdown）
          <textarea v-model="editing.description" rows="6"></textarea>
        </label>
        <label>
          单价（资金）
          <input v-model="editing.unit_price" :disabled="editing.status === 'active'" />
          <span v-if="editing.status === 'active'" class="hint">
            active 批次不可改价
          </span>
        </label>
        <div class="modal-actions">
          <button class="btn-secondary" @click="editing = null">取消</button>
          <button class="btn-primary" @click="save">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { padding: 16px; max-width: 1200px; margin: 0 auto; }
.page-title { font-size: 22px; font-weight: 700; margin-bottom: 16px; }
.hint { color: #999; font-size: 12px; margin-top: 8px; }
.table-wrap { margin-top: 16px; overflow-x: auto; border: 2px solid #000; box-shadow: 4px 4px 0 #000; background: #fff; }
.table { width: 100%; border-collapse: collapse; background: #fff; }
.table th, .table td { border: 1px solid #ccc; padding: 8px 12px; text-align: left; white-space: nowrap; }
.table th { background: #000; color: #fff; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; }
.table td:nth-child(4), .table td:nth-child(6) { font-variant-numeric: tabular-nums; }
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
.btn-sm {
  background: #fff; border: 1px solid #000; padding: 4px 12px;
  margin-right: 4px; cursor: pointer; font-family: inherit;
}
.btn-sm:hover { background: #000; color: #fff; }
.modal-bg {
  position: fixed; inset: 0; background: rgba(0,0,0,0.4);
  display: flex; align-items: center; justify-content: center; z-index: 1000;
  padding: 16px;
}
.modal {
  background: #fff; border: 3px solid #000; padding: 24px;
  width: 100%; max-width: 640px; box-shadow: 6px 6px 0 #000;
  max-height: calc(100vh - 32px); overflow-y: auto;
}
.modal h3 { font-size: 18px; font-weight: 700; margin-bottom: 16px; }
.modal label { display: block; margin: 12px 0; font-size: 13px; }
.modal input, .modal textarea, .modal select {
  width: 100%; padding: 6px; border: 1px solid #000; font-family: inherit;
}
.modal-actions { display: flex; gap: 8px; margin-top: 16px; justify-content: flex-end; }
</style>
