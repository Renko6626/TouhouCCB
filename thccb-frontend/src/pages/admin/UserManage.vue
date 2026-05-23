<script setup lang="ts">
import { ref, onMounted, computed, h } from 'vue'
import {
  useMessage, NDataTable, NButton, NInput, NModal, NTabs, NTabPane,
  NSelect, NTag,
  type DataTableColumns,
} from 'naive-ui'
import { adminApi, adminTitleApi, type UserListItem } from '@/api/admin'
import type { TitleRead } from '@/api/title'
import TitleChip from '@/components/title/TitleChip.vue'
import api from '@/api'

interface UserTitleRow {
  title: TitleRead
  granted_at: string
  source: string
  granted_by_admin_id: number | null
}

interface AdminUserSummaryDTO {
  user_id: number
  username: string
  email: string
  cash: number
  debt: number
  is_active: boolean
  is_superuser: boolean
  equipped_title_id: number | null
}

const message = useMessage()
const users = ref<UserListItem[]>([])
const allTitles = ref<TitleRead[]>([])
const filter = ref('')

const selected = ref<UserListItem | null>(null)
const userSummary = ref<AdminUserSummaryDTO | null>(null)
const userTitles = ref<UserTitleRow[]>([])
const showPanel = ref(false)

const adjustAmount = ref('')
const adjustReason = ref('')
const adjustLoading = ref(false)

const grantTitleId = ref<number | null>(null)
const grantLoading = ref(false)

const banLoading = ref(false)

async function refresh() {
  try {
    users.value = await adminApi.listUsers()
    allTitles.value = await adminTitleApi.listTitles()
  } catch (e) {
    message.error((e as { message?: string })?.message || '加载失败')
  }
}

const filtered = computed(() => {
  const q = filter.value.trim().toLowerCase()
  if (!q) return users.value
  return users.value.filter(
    (u) =>
      String(u.id).includes(q) || u.username.toLowerCase().includes(q)
  )
})

async function openUser(u: UserListItem) {
  selected.value = u
  userSummary.value = null
  userTitles.value = []
  showPanel.value = true
  try {
    userSummary.value = await adminTitleApi.userSummary(u.id)
    userTitles.value = await adminTitleApi.listUserTitles(u.id)
  } catch (e) {
    message.error((e as { message?: string })?.message || '加载用户详情失败')
  }
}

async function reloadUserDetails() {
  if (!selected.value) return
  userSummary.value = await adminTitleApi.userSummary(selected.value.id)
  userTitles.value = await adminTitleApi.listUserTitles(selected.value.id)
}

async function doAdjustCash() {
  if (!selected.value || !adjustAmount.value || !adjustReason.value.trim()) {
    message.error('金额和原因都要填')
    return
  }
  adjustLoading.value = true
  try {
    await api.post(`/api/v1/user/${selected.value.id}/adjust-cash`, {
      amount: adjustAmount.value,
      reason: adjustReason.value,
    })
    adjustAmount.value = ''
    adjustReason.value = ''
    await reloadUserDetails()
    message.success('调整成功')
  } catch (e) {
    message.error((e as { message?: string })?.message || '操作失败')
  } finally {
    adjustLoading.value = false
  }
}

async function doGrant() {
  if (!selected.value || !grantTitleId.value) return
  grantLoading.value = true
  try {
    await adminTitleApi.grantTitle(selected.value.id, grantTitleId.value)
    grantTitleId.value = null
    userTitles.value = await adminTitleApi.listUserTitles(selected.value.id)
    message.success('已授予')
  } catch (e) {
    message.error((e as { message?: string })?.message || '授予失败')
  } finally {
    grantLoading.value = false
  }
}

async function doRevoke(tid: number) {
  if (!selected.value) return
  if (!confirm('确认撤销该称号？')) return
  try {
    await adminTitleApi.revokeTitle(selected.value.id, tid)
    await reloadUserDetails()
    message.success('已撤销')
  } catch (e) {
    message.error((e as { message?: string })?.message || '撤销失败')
  }
}

async function doBan() {
  if (!selected.value) return
  if (!confirm(`确认封禁 ${selected.value.username}？`)) return
  banLoading.value = true
  try {
    await api.patch(`/api/v1/user/${selected.value.id}/ban`, { reason: '' })
    await refresh()
    await reloadUserDetails()
    message.success('已封禁')
  } catch (e) {
    message.error((e as { message?: string })?.message || '封禁失败')
  } finally {
    banLoading.value = false
  }
}

async function doUnban() {
  if (!selected.value) return
  banLoading.value = true
  try {
    await api.patch(`/api/v1/user/${selected.value.id}/unban`, {})
    await refresh()
    await reloadUserDetails()
    message.success('已解封')
  } catch (e) {
    message.error((e as { message?: string })?.message || '解封失败')
  } finally {
    banLoading.value = false
  }
}

const titleOptions = computed(() =>
  allTitles.value
    .filter((t: TitleRead) => t.is_active)
    .filter(
      (t: TitleRead) =>
        !userTitles.value.some((ut: UserTitleRow) => ut.title.id === t.id)
    )
    .map((t: TitleRead) => ({ label: t.name, value: t.id }))
)

const columns: DataTableColumns<UserListItem> = [
  { title: 'ID', key: 'id', width: 60 },
  { title: '用户名', key: 'username' },
  { title: 'Cash', key: 'cash', width: 100 },
  { title: 'Debt', key: 'debt', width: 100 },
  {
    title: '状态',
    key: '_status',
    width: 140,
    render(row) {
      return h('div', { class: 'flex gap-1' }, [
        row.is_active
          ? null
          : h(NTag, { type: 'error', size: 'small' }, () => '封禁'),
        row.is_superuser
          ? h(NTag, { type: 'warning', size: 'small' }, () => '管理员')
          : null,
      ])
    },
  },
  {
    title: '操作',
    key: '_act',
    width: 100,
    render(row) {
      return h(
        NButton,
        { size: 'small', onClick: () => openUser(row) },
        () => '管理'
      )
    },
  },
]

onMounted(refresh)
</script>

<template>
  <div class="p-6">
    <h1 class="text-2xl font-black border-b-4 border-black pb-1 mb-4">用户管理</h1>
    <NInput
      v-model:value="filter"
      placeholder="搜索 username / id"
      class="mb-3"
      clearable
    />
    <NDataTable
      :columns="columns"
      :data="filtered"
      :bordered="true"
      :max-height="600"
      :row-key="(row: UserListItem) => row.id"
    />

    <NModal
      v-model:show="showPanel"
      preset="card"
      :title="`用户 #${selected?.id} ${selected?.username ?? ''}`"
      style="max-width:680px;"
    >
      <div
        v-if="userSummary"
        class="mb-3 text-sm border-2 border-black p-3"
        style="box-shadow:4px 4px 0 #000;"
      >
        <div>
          Cash: <strong>{{ userSummary.cash }}</strong>
          &nbsp; Debt: <strong>{{ userSummary.debt }}</strong>
        </div>
        <div>
          状态: {{ userSummary.is_active ? '正常' : '已封禁' }}
          {{ userSummary.is_superuser ? '/ 管理员' : '' }}
        </div>
      </div>

      <NTabs default-value="title">
        <NTabPane name="title" tab="称号">
          <div class="space-y-2">
            <div
              v-for="ut in userTitles"
              :key="ut.title.id"
              class="flex justify-between items-center border-2 border-black p-2"
            >
              <div class="flex items-center gap-3">
                <TitleChip :title="ut.title" />
                <span class="text-xs text-gray-600">来源: {{ ut.source }}</span>
              </div>
              <NButton size="small" type="error" @click="doRevoke(ut.title.id)">
                撤销
              </NButton>
            </div>
            <div class="flex gap-2 mt-3">
              <NSelect
                v-model:value="grantTitleId"
                :options="titleOptions"
                placeholder="选称号授予"
                class="flex-1"
                clearable
              />
              <NButton
                type="primary"
                :loading="grantLoading"
                :disabled="!grantTitleId"
                @click="doGrant"
              >
                授予
              </NButton>
            </div>
          </div>
        </NTabPane>

        <NTabPane name="cash" tab="调现金">
          <NInput
            v-model:value="adjustAmount"
            placeholder="正数加 / 负数扣（如 100 或 -50）"
            class="mb-2"
          />
          <NInput
            v-model:value="adjustReason"
            placeholder="原因（必填，审计用）"
          />
          <NButton
            type="primary"
            class="mt-3"
            :loading="adjustLoading"
            @click="doAdjustCash"
          >
            提交
          </NButton>
        </NTabPane>

        <NTabPane name="ban" tab="封禁">
          <div class="space-y-2">
            <NButton
              v-if="userSummary?.is_active"
              type="error"
              :loading="banLoading"
              @click="doBan"
            >
              封禁此用户
            </NButton>
            <NButton
              v-else
              type="warning"
              :loading="banLoading"
              @click="doUnban"
            >
              解封此用户
            </NButton>
          </div>
        </NTabPane>
      </NTabs>

      <div class="text-xs text-gray-500 mt-4">
        借款 / 免债 / 提撤管理员 等操作请使用对应专门页面（后续可整合进来）。
      </div>
    </NModal>
  </div>
</template>
