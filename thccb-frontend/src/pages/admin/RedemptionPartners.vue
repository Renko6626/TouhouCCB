<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { redemptionAdminApi } from '@/api/redemption'
import type { PartnerAdminItem } from '@/types/redemption'

interface PartnerForm {
  id?: number
  name: string
  description: string
  website_url: string
  logo_url: string
  is_active: boolean
}

const items = ref<PartnerAdminItem[]>([])
const editing = ref<PartnerForm | null>(null)

async function load() {
  items.value = await redemptionAdminApi.listPartners()
}

function startCreate() {
  editing.value = {
    name: '', description: '', website_url: '', logo_url: '', is_active: true,
  }
}

function startEdit(p: PartnerAdminItem) {
  editing.value = {
    id: p.id, name: p.name, description: p.description,
    website_url: p.website_url, logo_url: p.logo_url ?? '',
    is_active: p.is_active,
  }
}

async function save() {
  if (!editing.value) return
  const f = editing.value
  const payload = {
    name: f.name,
    description: f.description,
    website_url: f.website_url,
    logo_url: f.logo_url || null,
    is_active: f.is_active,
  }
  if (f.id !== undefined) {
    await redemptionAdminApi.updatePartner(f.id, payload)
  } else {
    await redemptionAdminApi.createPartner(payload)
  }
  editing.value = null
  await load()
}

async function toggleActive(p: PartnerAdminItem) {
  await redemptionAdminApi.updatePartner(p.id, { is_active: !p.is_active })
  await load()
}

onMounted(load)
</script>

<template>
  <div class="page">
    <h1 class="page-title">合作方管理</h1>
    <button class="btn-primary" @click="startCreate">+ 新增合作方</button>

    <div class="table-wrap">
    <table class="table">
      <thead>
        <tr><th>ID</th><th>名称</th><th>网站</th><th>启用</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="p in items" :key="p.id">
          <td>{{ p.id }}</td>
          <td>{{ p.name }}</td>
          <td>
            <a v-if="p.website_url" :href="p.website_url" target="_blank" rel="noopener">
              {{ p.website_url }}
            </a>
          </td>
          <td>{{ p.is_active ? '✓' : '✕' }}</td>
          <td>
            <button class="btn-sm" @click="startEdit(p)">编辑</button>
            <button class="btn-sm" @click="toggleActive(p)">
              {{ p.is_active ? '禁用' : '启用' }}
            </button>
          </td>
        </tr>
      </tbody>
    </table>
    </div>

    <div v-if="editing" class="modal-bg" @click.self="editing = null">
      <div class="modal">
        <h3>{{ editing.id !== undefined ? '编辑' : '新增' }}合作方</h3>
        <label>名称 <input v-model="editing.name" /></label>
        <label>描述 <textarea v-model="editing.description" rows="3"></textarea></label>
        <label>网站 URL <input v-model="editing.website_url" /></label>
        <label>Logo URL <input v-model="editing.logo_url" /></label>
        <label class="checkbox-row">
          <input type="checkbox" v-model="editing.is_active" /> 启用
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
.page { padding: 16px; max-width: 1100px; margin: 0 auto; }
.page-title { font-size: 22px; font-weight: 700; margin-bottom: 16px; }
.table-wrap { margin-top: 16px; overflow-x: auto; border: 2px solid #000; box-shadow: 4px 4px 0 #000; background: #fff; }
.table { width: 100%; border-collapse: collapse; background: #fff; }
.table th, .table td { border: 1px solid #ccc; padding: 8px 12px; text-align: left; white-space: nowrap; }
.table th { background: #000; color: #fff; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; }
.btn-primary, .btn-secondary {
  border: 2px solid #000; padding: 8px 20px; cursor: pointer;
  font-weight: 600; font-family: inherit;
  transition: transform 0.1s, box-shadow 0.1s;
}
.btn-primary { background: #000; color: #fff; box-shadow: 4px 4px 0 #444; }
.btn-primary:hover { transform: translate(-1px, -1px); box-shadow: 5px 5px 0 #444; }
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
  width: 100%; max-width: 520px; box-shadow: 6px 6px 0 #000;
}
.modal h3 { font-size: 18px; font-weight: 700; margin-bottom: 16px; }
.modal label { display: block; margin: 12px 0; font-size: 13px; }
.modal input, .modal textarea {
  width: 100%; padding: 6px; border: 1px solid #000; font-family: inherit;
}
.checkbox-row { display: flex; gap: 8px; align-items: center; }
.checkbox-row input { width: auto; }
.modal-actions { display: flex; gap: 8px; margin-top: 16px; justify-content: flex-end; }
</style>
