<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { redemptionApi } from '@/api/redemption'
import type { MyRedemptionItem, MyRedemptionDetail } from '@/types/redemption'

const items = ref<MyRedemptionItem[]>([])
const expanded = ref<Map<number, MyRedemptionDetail>>(new Map())
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    items.value = await redemptionApi.myRedemptions()
  } finally {
    loading.value = false
  }
}

async function toggle(id: number) {
  if (expanded.value.has(id)) {
    expanded.value.delete(id)
    expanded.value = new Map(expanded.value)
    return
  }
  const detail = await redemptionApi.myRedemptionDetail(id)
  expanded.value.set(id, detail)
  expanded.value = new Map(expanded.value)
}

async function copyCode(code: string) {
  await navigator.clipboard.writeText(code)
  alert('已复制')
}

async function toggleUsed(item: MyRedemptionItem) {
  const newState = !item.marked_used_by_user_at
  const r = await redemptionApi.markUsed(item.code_id, newState)
  item.marked_used_by_user_at = r.marked_used_by_user_at
}

onMounted(load)
</script>

<template>
  <div class="page">
    <h1 class="page-title">我的兑换</h1>

    <div v-if="loading" class="loading">加载中…</div>
    <div v-else-if="items.length === 0" class="empty">尚未兑换任何码</div>

    <ul v-else class="list">
      <li
        v-for="item in items"
        :key="item.code_id"
        class="row"
        :class="{ used: !!item.marked_used_by_user_at }"
      >
        <div class="row-head" @click="toggle(item.code_id)">
          <div class="col-name">
            <div class="name">{{ item.batch_name }}</div>
            <div class="partner">{{ item.partner_name }}</div>
          </div>
          <div class="col-meta">
            <span>{{ new Date(item.bought_at).toLocaleString() }}</span>
            <span>{{ item.paid_amount }}</span>
            <span class="status">{{ item.marked_used_by_user_at ? '已使用' : '未使用' }}</span>
          </div>
        </div>

        <div v-if="expanded.get(item.code_id)" class="row-detail">
          <div class="code-box">{{ expanded.get(item.code_id)!.code_string }}</div>
          <button class="btn" @click="copyCode(expanded.get(item.code_id)!.code_string)">
            复制码
          </button>
          <a
            v-if="item.partner_website_url"
            :href="item.partner_website_url"
            target="_blank"
            rel="noopener"
            class="btn"
          >
            前往 {{ item.partner_name }} →
          </a>
          <button class="btn" @click="toggleUsed(item)">
            {{ item.marked_used_by_user_at ? '取消已用标记' : '标记为已使用' }}
          </button>
          <pre class="description">{{ expanded.get(item.code_id)!.description }}</pre>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.page { padding: 16px; max-width: 960px; margin: 0 auto; }
.page-title { font-size: 22px; font-weight: 700; margin-bottom: 16px; }
.loading, .empty { color: #999; padding: 32px; text-align: center; }
.list { list-style: none; padding: 0; }
.row {
  border: 2px solid #000; margin-bottom: 12px; background: #fff;
  box-shadow: 4px 4px 0 #000;
}
.row.used { opacity: 0.6; }
.row-head {
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 16px; cursor: pointer; gap: 12px;
}
.row-head:hover { background: #f5f5f5; }
.name { font-weight: 600; }
.partner { font-size: 12px; color: #666; }
.col-meta {
  display: flex; gap: 12px; font-size: 12px; color: #555;
  flex-wrap: wrap; justify-content: flex-end;
  font-variant-numeric: tabular-nums;
}
.status { font-weight: 700; color: #000; }
.row-detail { padding: 16px; border-top: 1px dashed #ccc; background: #fafafa; }
.code-box {
  font-family: monospace; font-size: 16px; padding: 12px; border: 2px dashed #000;
  margin-bottom: 12px; word-break: break-all; background: #fff;
}
.btn {
  background: #fff; color: #000; border: 2px solid #000; padding: 6px 16px;
  cursor: pointer; font-size: 13px; margin-right: 8px; text-decoration: none;
  display: inline-block; font-family: inherit;
  box-shadow: 2px 2px 0 #000;
  transition: transform 0.1s, box-shadow 0.1s, background 0.1s, color 0.1s;
}
.btn:hover {
  background: #000; color: #fff;
  transform: translate(-1px, -1px); box-shadow: 3px 3px 0 #000;
}
.description {
  white-space: pre-wrap; font-family: inherit;
  margin-top: 12px; font-size: 13px; color: #555;
}
</style>
