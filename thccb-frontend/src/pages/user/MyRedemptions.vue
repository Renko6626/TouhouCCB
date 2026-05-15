<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { redemptionApi } from '@/api/redemption'
import { danmukuApi, type DanmukuExchangeHistoryItem } from '@/api/danmuku'
import type { MyRedemptionItem, MyRedemptionDetail } from '@/types/redemption'

const message = useMessage()

// 两种兑换来源用 discriminated union 表达，方便模板按 source 分支渲染
type PartnerRow = {
  source: 'partner'
  ts: number
  payload: MyRedemptionItem
}
type DanmukuRow = {
  source: 'danmuku'
  ts: number
  payload: DanmukuExchangeHistoryItem
}
type Row = PartnerRow | DanmukuRow

type Filter = 'all' | 'partner' | 'danmuku'

const partnerItems = ref<MyRedemptionItem[]>([])
const danmukuItems = ref<DanmukuExchangeHistoryItem[]>([])
const expanded = ref<Map<string, MyRedemptionDetail>>(new Map())
const loading = ref(false)
const filter = ref<Filter>('all')

const filters: { value: Filter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'partner', label: '合作方' },
  { value: 'danmuku', label: '弹幕' },
]

const allRows = computed<Row[]>(() => {
  const rows: Row[] = []
  for (const p of partnerItems.value) {
    rows.push({ source: 'partner', ts: new Date(p.bought_at).getTime(), payload: p })
  }
  for (const d of danmukuItems.value) {
    rows.push({ source: 'danmuku', ts: new Date(d.timestamp).getTime(), payload: d })
  }
  rows.sort((a, b) => b.ts - a.ts)
  return rows
})

const filteredRows = computed(() => {
  if (filter.value === 'all') return allRows.value
  return allRows.value.filter(r => r.source === filter.value)
})

async function load() {
  loading.value = true
  try {
    const [p, d] = await Promise.all([
      redemptionApi.myRedemptions().catch(() => []),
      danmukuApi.myHistory().catch(() => []),
    ])
    partnerItems.value = p
    danmukuItems.value = d
  } finally {
    loading.value = false
  }
}

// 合作方兑换的"展开详情"——单独再拉一次 detail 拿 code_string
async function togglePartnerDetail(codeId: number) {
  const key = `partner:${codeId}`
  if (expanded.value.has(key)) {
    expanded.value.delete(key)
    expanded.value = new Map(expanded.value)
    return
  }
  const detail = await redemptionApi.myRedemptionDetail(codeId)
  expanded.value.set(key, detail)
  expanded.value = new Map(expanded.value)
}

function toggleDanmukuDetail(id: number) {
  const key = `danmuku:${id}`
  if (expanded.value.has(key)) {
    expanded.value.delete(key)
  } else {
    // 弹幕历史本身就含 code_string，不需要再拉接口；用 partial 对象占位
    expanded.value.set(key, {} as MyRedemptionDetail)
  }
  expanded.value = new Map(expanded.value)
}

async function copyCode(code: string) {
  await navigator.clipboard.writeText(code)
  message.success('激活码已复制')
}

async function toggleUsed(item: MyRedemptionItem) {
  const newState = !item.marked_used_by_user_at
  const r = await redemptionApi.markUsed(item.code_id, newState)
  item.marked_used_by_user_at = r.marked_used_by_user_at
}

const fmtTime = (s: string) => new Date(s).toLocaleString('zh-CN')

onMounted(load)
</script>

<template>
  <div class="page">
    <header class="page-header">
      <h1 class="page-title">我的兑换</h1>
      <p class="page-sub">合作方兑换 + 弹幕兑换合并展示，按时间倒序</p>
    </header>

    <!-- 来源筛选 -->
    <div class="filter-tabs">
      <button
        v-for="opt in filters"
        :key="opt.value"
        type="button"
        class="filter-tab"
        :class="{ 'filter-tab--active': filter === opt.value }"
        @click="filter = opt.value"
      >
        {{ opt.label }}
      </button>
    </div>

    <div v-if="loading" class="state">加载中…</div>
    <div v-else-if="filteredRows.length === 0" class="state">
      {{ filter === 'all' ? '尚未兑换任何记录' : '该来源下没有记录' }}
    </div>

    <ul v-else class="list">
      <li
        v-for="row in filteredRows"
        :key="`${row.source}:${row.source === 'partner' ? row.payload.code_id : row.payload.id}`"
        class="row"
        :class="{ used: row.source === 'partner' && !!row.payload.marked_used_by_user_at }"
      >
        <!-- 合作方兑换行 -->
        <template v-if="row.source === 'partner'">
          <div class="row-head" @click="togglePartnerDetail(row.payload.code_id)">
            <div class="col-name">
              <span class="source-tag source-tag--partner">合作方</span>
              <div class="name">{{ row.payload.batch_name }}</div>
              <div class="sub">{{ row.payload.partner_name }}</div>
            </div>
            <div class="col-meta">
              <span class="meta-time">{{ fmtTime(row.payload.bought_at) }}</span>
              <span class="meta-amount tabular-nums">¥ {{ row.payload.paid_amount }}</span>
              <span class="meta-status">
                {{ row.payload.marked_used_by_user_at ? '已使用' : '未使用' }}
              </span>
            </div>
          </div>
          <div v-if="expanded.get(`partner:${row.payload.code_id}`)" class="row-detail">
            <div class="code-box">{{ expanded.get(`partner:${row.payload.code_id}`)!.code_string }}</div>
            <div class="action-row">
              <button class="btn-secondary" @click="copyCode(expanded.get(`partner:${row.payload.code_id}`)!.code_string)">
                复制码
              </button>
              <a
                v-if="row.payload.partner_website_url"
                :href="row.payload.partner_website_url"
                target="_blank"
                rel="noopener"
                class="btn-secondary"
              >前往 {{ row.payload.partner_name }} →</a>
              <button class="btn-secondary" @click="toggleUsed(row.payload)">
                {{ row.payload.marked_used_by_user_at ? '取消已用标记' : '标记为已使用' }}
              </button>
            </div>
            <pre v-if="expanded.get(`partner:${row.payload.code_id}`)!.description" class="description">{{ expanded.get(`partner:${row.payload.code_id}`)!.description }}</pre>
          </div>
        </template>

        <!-- 弹幕兑换行 -->
        <template v-else>
          <div class="row-head" @click="toggleDanmukuDetail(row.payload.id)">
            <div class="col-name">
              <span class="source-tag source-tag--danmuku">弹幕</span>
              <div class="name">弹幕系统兑换</div>
              <div class="sub">QQ {{ row.payload.qq_user_id }} · 房间 {{ row.payload.room_id }}</div>
            </div>
            <div class="col-meta">
              <span class="meta-time">{{ fmtTime(row.payload.timestamp) }}</span>
              <span class="meta-amount tabular-nums">¥ {{ Number(row.payload.amount).toFixed(2) }}</span>
              <span class="meta-mini tabular-nums">
                y {{ Number(row.payload.yuan).toFixed(2) }} · h {{ Number(row.payload.huo).toFixed(2) }}
              </span>
            </div>
          </div>
          <div v-if="expanded.get(`danmuku:${row.payload.id}`)" class="row-detail">
            <div class="code-box">{{ row.payload.code_string }}</div>
            <div class="action-row">
              <button class="btn-secondary" @click="copyCode(row.payload.code_string)">
                复制激活码
              </button>
            </div>
          </div>
        </template>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.page {
  padding: 16px;
  max-width: 960px;
  margin: 0 auto;
}

.page-header {
  margin-bottom: 16px;
}
.page-title {
  font-size: 22px;
  font-weight: 800;
  letter-spacing: 0.02em;
  color: #000;
}
.page-sub {
  margin-top: 4px;
  font-size: 13px;
  color: #666;
}

/* 筛选 tab —— 与 Leaderboard 一致 */
.filter-tabs {
  display: flex;
  gap: 0;
  margin-bottom: 16px;
  border-bottom: 2px solid #000;
}
.filter-tab {
  appearance: none;
  background: transparent;
  border: 2px solid #000;
  border-bottom: none;
  padding: 6px 16px;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: #000;
  cursor: pointer;
  margin-right: -2px;
  margin-bottom: -2px;
}
.filter-tab:hover {
  background: #f0f0f0;
}
.filter-tab--active {
  background: #000;
  color: #fff;
}

.state {
  padding: 48px 0;
  text-align: center;
  font-size: 13px;
  color: #999;
}

.list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.row {
  border: 2px solid #000;
  margin-bottom: 12px;
  background: #fff;
  box-shadow: 4px 4px 0 #000;
}
.row.used {
  opacity: 0.55;
}
.row-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  cursor: pointer;
  gap: 12px;
}
.row-head:hover {
  background: #f5f5f5;
}

.col-name {
  min-width: 0;
}
.source-tag {
  display: inline-block;
  font-size: 10px;
  font-weight: 800;
  padding: 1px 6px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  border: 1.5px solid #000;
  margin-right: 6px;
  vertical-align: middle;
}
.source-tag--partner {
  background: #fff;
  color: #000;
}
.source-tag--danmuku {
  background: #000;
  color: #fff;
}

.name {
  font-weight: 600;
  font-size: 14px;
  margin-top: 2px;
}
.sub {
  font-size: 12px;
  color: #666;
  margin-top: 2px;
}

.col-meta {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 3px;
  font-size: 12px;
  color: #555;
  flex-shrink: 0;
}
.meta-time {
  color: #888;
}
.meta-amount {
  font-weight: 700;
  color: #000;
  font-size: 13px;
}
.meta-status {
  font-weight: 700;
  color: #000;
}
.meta-mini {
  color: #777;
  font-size: 11px;
}

.row-detail {
  padding: 16px;
  border-top: 1px solid #e0e0e0;
  background: #fafafa;
}
.code-box {
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-size: 12px;
  padding: 10px 12px;
  border: 1.5px solid #000;
  margin-bottom: 12px;
  word-break: break-all;
  background: #fff;
}

.action-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.btn-secondary {
  appearance: none;
  font-family: inherit;
  display: inline-flex;
  align-items: center;
  padding: 5px 14px;
  font-size: 12px;
  font-weight: 700;
  background: #fff;
  color: #000;
  border: 1.5px solid #000;
  cursor: pointer;
  text-decoration: none;
  transition: background 0.1s, color 0.1s;
}
.btn-secondary:hover {
  background: #000;
  color: #fff;
}

.description {
  white-space: pre-wrap;
  font-family: inherit;
  margin-top: 8px;
  font-size: 12px;
  color: #555;
}

@media (max-width: 540px) {
  .row-head {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }
  .col-meta {
    align-items: flex-start;
    width: 100%;
  }
}
</style>
