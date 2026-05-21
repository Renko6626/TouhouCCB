<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  NButton, NInput, NInputNumber, NTable, NTag, NSpin, NAlert,
  NSelect, NDivider, useMessage, useDialog,
  type SelectOption,
} from 'naive-ui'
import {
  adminApi,
  type BotSuspicionItem, type BannedUserItem, type BotStats,
} from '@/api/admin'

const msg = useMessage()
const dialog = useDialog()

const suspicions = ref<BotSuspicionItem[]>([])
const banned = ref<BannedUserItem[]>([])
const stats = ref<BotStats | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)

const filterStatus = ref<'pending' | 'reviewed' | 'all'>('pending')
const filterOptions: SelectOption[] = [
  { label: '待审 (pending)', value: 'pending' },
  { label: '已审 (reviewed)', value: 'reviewed' },
  { label: '全部', value: 'all' },
]

// 手动封号表单
const manualUserId = ref<number | null>(null)
const manualReason = ref<string>('')

async function refresh() {
  loading.value = true
  error.value = null
  try {
    const [s, b, st] = await Promise.all([
      adminApi.listBotSuspicions(filterStatus.value, 100),
      adminApi.listBannedUsers(200),
      adminApi.botStats(),
    ])
    suspicions.value = s
    banned.value = b
    stats.value = st
  } catch (e: unknown) {
    error.value = (e as { message?: string })?.message ?? '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(refresh)

function formatTs(iso: string): string {
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}

function formatSignals(s: string): string[] {
  return s.split(',').map(x => x.trim()).filter(Boolean)
}

// ── 封号（带原因，封后顺手 mark suspicion as confirmed_bot）──
async function confirmBotAndBan(s: BotSuspicionItem) {
  const reasonRef = { v: '' }
  dialog.warning({
    title: `确认 Bot 并封号: ${s.username}`,
    content: () => buildReasonInputContent(reasonRef, `信号: ${s.signals}`),
    positiveText: '确认并封号',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await adminApi.banUser(s.user_id, reasonRef.v || undefined, s.id)
        await adminApi.reviewSuspicion(s.id, 'confirmed_bot', reasonRef.v || undefined)
        msg.success(`已封号 ${s.username} (user_id=${s.user_id}) 并标记 confirmed_bot`)
        await refresh()
      } catch (e: unknown) {
        msg.error((e as { data?: { detail?: string }; message?: string })?.data?.detail ?? (e as { message?: string })?.message ?? '操作失败')
      }
    },
  })
}

// ── 标记误报（不封号）──
async function markFalsePositive(s: BotSuspicionItem) {
  try {
    await adminApi.reviewSuspicion(s.id, 'false_positive')
    msg.success(`已标记 #${s.id} 为误报`)
    await refresh()
  } catch (e: unknown) {
    msg.error((e as { data?: { detail?: string }; message?: string })?.data?.detail ?? '失败')
  }
}

// ── 仅封号（不动 suspicion 审核状态）──
async function banOnly(s: BotSuspicionItem) {
  const reasonRef = { v: '' }
  dialog.warning({
    title: `仅封号（不标记 suspicion）: ${s.username}`,
    content: () => buildReasonInputContent(reasonRef, 'suspicion 仍保持 pending'),
    positiveText: '封号',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await adminApi.banUser(s.user_id, reasonRef.v || undefined, s.id)
        msg.success(`已封号 ${s.username}`)
        await refresh()
      } catch (e: unknown) {
        msg.error((e as { data?: { detail?: string }; message?: string })?.data?.detail ?? '失败')
      }
    },
  })
}

// ── 解封 ──
async function unban(userId: number, username: string) {
  dialog.warning({
    title: `解封 ${username}`,
    content: `确认解封 user_id=${userId}？解封后该用户能重新交易。`,
    positiveText: '解封',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await adminApi.unbanUser(userId)
        msg.success(`已解封 ${username}`)
        await refresh()
      } catch (e: unknown) {
        msg.error((e as { data?: { detail?: string }; message?: string })?.data?.detail ?? '失败')
      }
    },
  })
}

// ── 手动封号 (无 suspicion 关联) ──
async function manualBan() {
  if (!manualUserId.value) {
    msg.error('请输入 user_id')
    return
  }
  const uid = manualUserId.value
  const reason = manualReason.value
  dialog.warning({
    title: `手动封号 user_id=${uid}`,
    content: `理由: ${reason || '<未填>'}。该操作不关联任何 BotSuspicion，会写 backend 日志审计。`,
    positiveText: '确认封号',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        const r = await adminApi.banUser(uid, reason || undefined)
        msg.success(`已封号 ${r.username} (user_id=${uid})`)
        manualUserId.value = null
        manualReason.value = ''
        await refresh()
      } catch (e: unknown) {
        msg.error((e as { data?: { detail?: string }; message?: string })?.data?.detail ?? '失败')
      }
    },
  })
}

// 用 h() 渲染 dialog 内嵌输入框
import { h } from 'vue'
function buildReasonInputContent(reasonRef: { v: string }, subtitle: string) {
  return h('div', { style: 'font-size: 13px;' }, [
    h('div', { style: 'margin-bottom: 8px; color: #555;' }, subtitle),
    h('div', { style: 'margin-bottom: 6px; font-weight: 600;' }, '封号原因（选填，写入 backend log）：'),
    h('input', {
      type: 'text',
      placeholder: '如：高频交易 / 多账号 / 异常 metrics ...',
      style: 'width: 100%; padding: 6px 10px; border: 2px solid #000; font-size: 13px;',
      onInput: (e: Event) => { reasonRef.v = (e.target as HTMLInputElement).value },
    }),
  ])
}

const reviewTagType = (s: string): 'default' | 'success' | 'warning' | 'error' => {
  if (s === 'pending') return 'warning'
  if (s === 'confirmed_bot') return 'error'
  if (s === 'false_positive') return 'success'
  return 'default'
}

const reviewTagLabel = (s: string): string => {
  if (s === 'pending') return '待审'
  if (s === 'confirmed_bot') return '已确认 bot'
  if (s === 'false_positive') return '误报'
  return s
}

const pendingCount = computed(() => stats.value?.pending_suspicions ?? '—')
const bannedCount = computed(() => stats.value?.banned_users ?? '—')
</script>

<template>
  <div class="bot-review-ban">
    <NSpin :show="loading">
      <NAlert v-if="error" type="error" :title="error" />

      <!-- 顶部统计 -->
      <section class="panel stats-panel">
        <div class="stats-row">
          <div class="stat-item">
            <span class="stat-label">待审 Bot 预警</span>
            <span class="stat-value liq-danger">{{ pendingCount }}</span>
          </div>
          <div class="stat-item">
            <span class="stat-label">已封禁用户</span>
            <span class="stat-value liq-danger">{{ bannedCount }}</span>
          </div>
          <NButton @click="refresh" size="small">刷新</NButton>
        </div>
      </section>

      <!-- Bot 预警列表 -->
      <section class="panel">
        <div class="section-head">
          <h2>Bot 预警审核</h2>
          <NSelect
            v-model:value="filterStatus"
            :options="filterOptions"
            size="small"
            style="width: 180px"
            @update:value="refresh"
          />
        </div>

        <NTable v-if="suspicions.length > 0" :bordered="true" :single-line="false" size="small">
          <thead>
            <tr>
              <th>ID</th>
              <th>用户</th>
              <th>触发时间</th>
              <th>命中信号</th>
              <th>审核状态</th>
              <th>封号状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in suspicions" :key="s.id">
              <td class="mono">#{{ s.id }}</td>
              <td>
                <div class="user-cell">
                  <strong>{{ s.username }}</strong>
                  <code class="user-id">uid={{ s.user_id }}</code>
                </div>
              </td>
              <td class="ts">{{ formatTs(s.triggered_at) }}</td>
              <td>
                <div class="signal-list">
                  <NTag v-for="sig in formatSignals(s.signals)" :key="sig" size="small" type="warning">{{ sig }}</NTag>
                </div>
              </td>
              <td><NTag :type="reviewTagType(s.review_status)" size="small">{{ reviewTagLabel(s.review_status) }}</NTag></td>
              <td>
                <NTag v-if="!s.user_is_active" type="error" size="small">已封禁</NTag>
                <NTag v-else type="success" size="small">活跃</NTag>
              </td>
              <td class="action-cell">
                <NButton
                  v-if="s.user_is_active"
                  size="tiny"
                  type="error"
                  @click="confirmBotAndBan(s)"
                >确认 bot + 封号</NButton>
                <NButton
                  v-if="s.user_is_active && s.review_status === 'pending'"
                  size="tiny"
                  @click="banOnly(s)"
                >仅封号</NButton>
                <NButton
                  v-if="s.review_status === 'pending'"
                  size="tiny"
                  type="success"
                  @click="markFalsePositive(s)"
                >误报</NButton>
                <NButton
                  v-if="!s.user_is_active"
                  size="tiny"
                  type="warning"
                  @click="unban(s.user_id, s.username)"
                >解封</NButton>
              </td>
            </tr>
          </tbody>
        </NTable>
        <div v-else class="empty-hint">当前筛选下无 Bot 预警记录</div>
      </section>

      <NDivider />

      <!-- 手动封号 -->
      <section class="panel">
        <h2>手动封号 / 解封</h2>
        <p class="hint">无 BotSuspicion 关联场景（你自己排查发现的恶意账号）。封后写 backend log 审计。</p>
        <div class="row">
          <NInputNumber
            v-model:value="manualUserId"
            placeholder="user_id"
            :precision="0"
            :min="1"
            style="width: 160px"
          />
          <NInput
            v-model:value="manualReason"
            placeholder="封号原因（选填）"
            style="flex: 1; max-width: 480px"
          />
          <NButton type="error" :disabled="!manualUserId" @click="manualBan">封号</NButton>
        </div>
      </section>

      <NDivider />

      <!-- 已封禁列表 -->
      <section class="panel">
        <h2>已封禁用户列表（{{ banned.length }} 人）</h2>
        <NTable v-if="banned.length > 0" :bordered="true" :single-line="false" size="small">
          <thead>
            <tr>
              <th>user_id</th>
              <th>username</th>
              <th>casdoor_id</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="u in banned" :key="u.user_id">
              <td class="mono">#{{ u.user_id }}</td>
              <td><strong>{{ u.username }}</strong></td>
              <td class="mono small">{{ u.casdoor_id ?? '—' }}</td>
              <td>
                <NButton size="tiny" type="warning" @click="unban(u.user_id, u.username)">解封</NButton>
              </td>
            </tr>
          </tbody>
        </NTable>
        <div v-else class="empty-hint">当前无封禁用户</div>
      </section>
    </NSpin>
  </div>
</template>

<style scoped>
.bot-review-ban {
  padding: 16px;
}
.panel {
  margin-bottom: 16px;
  border: 2px solid #000;
  padding: 16px;
  background: #fff;
}
.stats-panel {
  background: #fafafa;
}
.stats-row {
  display: flex;
  align-items: center;
  gap: 24px;
  flex-wrap: wrap;
}
.stat-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.stat-label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: #666;
}
.stat-value {
  font-size: 24px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
}
.liq-danger {
  color: #cc0000;
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
  flex-wrap: wrap;
  gap: 8px;
}
h2 {
  margin: 0 0 8px 0;
}
.user-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.user-id {
  font-family: monospace;
  font-size: 10px;
  color: #888;
}
.signal-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.action-cell {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}
.ts {
  font-size: 12px;
  color: #555;
  white-space: nowrap;
}
.mono {
  font-family: monospace;
  font-size: 12px;
}
.small {
  font-size: 11px;
  color: #888;
}
.empty-hint {
  padding: 24px 12px;
  text-align: center;
  color: #888;
  border: 1.5px dashed #ccc;
  font-size: 13px;
}
.row {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin-top: 8px;
}
.hint {
  font-size: 12px;
  color: #666;
  margin: 0 0 6px 0;
}
</style>
