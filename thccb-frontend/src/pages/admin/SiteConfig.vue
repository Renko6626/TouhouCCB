<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  NTable, NInput, NButton, NSpin, NAlert, useMessage,
  NInputNumber, NDivider, NSelect, type SelectOption,
} from 'naive-ui'
import { adminSiteConfigApi, type SiteConfigItem } from '@/api/loan'
import { adminApi, type UserListItem } from '@/api/admin'

const configs = ref<SiteConfigItem[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const drafts = ref<Record<string, string>>({})
const msg = useMessage()

// 用户列表（用于下拉）
const userList = ref<UserListItem[]>([])

const userOptions = computed<SelectOption[]>(() =>
  userList.value.map(u => ({
    label: `#${u.id}  ${u.username}  (现金 ¥${u.cash.toFixed(2)} / 负债 ¥${u.debt.toFixed(2)})`,
    value: u.id,
  })),
)

const selectedUser = computed(() =>
  userList.value.find(u => u.id === targetUserId.value) ?? null,
)

// 强制放贷 / 免债表单
const targetUserId = ref<number | null>(null)
const forceAmount = ref<number | null>(null)
const forceReason = ref<string>('')
const forgiveAmount = ref<number | null>(null)
const forgiveReason = ref<string>('')

async function load() {
  loading.value = true
  error.value = null
  try {
    configs.value = await adminSiteConfigApi.list()
    drafts.value = Object.fromEntries(configs.value.map(c => [c.key, c.value]))
  } catch (e: any) {
    error.value = e?.message ?? '加载失败'
  } finally {
    loading.value = false
  }
}

async function save(key: string) {
  try {
    await adminSiteConfigApi.update(key, drafts.value[key])
    msg.success(`${key} 已更新`)
    await load()
  } catch (e: any) {
    msg.error(e?.message ?? '更新失败')
  }
}

async function loadUsers() {
  try {
    userList.value = await adminApi.listUsers()
  } catch (e) {
    msg.error(e instanceof Error ? e.message : '加载用户列表失败')
  }
}

async function doForceLoan() {
  if (!targetUserId.value) return msg.error('请选择目标用户')
  if (!forceAmount.value || forceAmount.value <= 0) return msg.error('金额必须大于 0')
  if (!forceReason.value.trim()) return msg.error('请填写原因')
  try {
    await adminSiteConfigApi.forceLoan(targetUserId.value, String(forceAmount.value), forceReason.value)
    msg.success('放贷成功')
    forceAmount.value = null
    forceReason.value = ''
    await loadUsers()
  } catch (e: any) {
    msg.error(e?.data?.detail ?? e?.message ?? '失败')
  }
}

async function doForgiveDebt() {
  if (!targetUserId.value) return msg.error('请选择目标用户')
  if (!forgiveAmount.value || forgiveAmount.value <= 0) return msg.error('金额必须大于 0')
  if (!forgiveReason.value.trim()) return msg.error('请填写原因')
  try {
    await adminSiteConfigApi.forgiveDebt(targetUserId.value, String(forgiveAmount.value), forgiveReason.value)
    msg.success('免债成功')
    forgiveAmount.value = null
    forgiveReason.value = ''
    await loadUsers()
  } catch (e: any) {
    msg.error(e?.data?.detail ?? e?.message ?? '失败')
  }
}

onMounted(async () => {
  await Promise.all([load(), loadUsers()])
})
</script>

<template>
  <div class="admin-site-config">
    <NSpin :show="loading">
      <NAlert v-if="error" type="error" :title="error" />

      <section class="panel">
        <h2>站点配置</h2>
        <NTable :bordered="true" :single-line="false" size="small">
          <thead>
            <tr>
              <th>Key</th>
              <th>类型</th>
              <th>当前值</th>
              <th>更新时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in configs" :key="c.key">
              <td><code>{{ c.key }}</code></td>
              <td>{{ c.value_type }}</td>
              <td><NInput v-model:value="drafts[c.key]" size="small" /></td>
              <td class="ts">{{ new Date(c.updated_at).toLocaleString() }}</td>
              <td>
                <NButton size="small" type="primary" @click="save(c.key)">保存</NButton>
              </td>
            </tr>
          </tbody>
        </NTable>
      </section>

      <NDivider />

      <section class="panel">
        <h2>强制放贷 / 免除债务</h2>
        <div class="row">
          <span class="lbl">目标用户：</span>
          <NSelect
            v-model:value="targetUserId"
            :options="userOptions"
            placeholder="选择用户"
            filterable
            clearable
            style="min-width: 360px; flex: 1; max-width: 520px"
          />
          <NButton size="small" @click="loadUsers">刷新</NButton>
        </div>
        <div v-if="selectedUser" class="user-snapshot">
          当前：<b>{{ selectedUser.username }}</b>（现金 ¥{{ selectedUser.cash.toFixed(2) }} / 负债 ¥{{ selectedUser.debt.toFixed(2) }}）
        </div>

        <h3>强制放贷（受 loan_enabled 约束）</h3>
        <div class="row">
          <NInputNumber
            v-model:value="forceAmount"
            placeholder="金额（>0）"
            :min="0.000001"
            :precision="2"
            style="width: 180px"
          />
          <NInput v-model:value="forceReason" placeholder="原因（必填）" style="flex: 1; max-width: 320px" />
          <NButton type="warning" :disabled="!targetUserId" @click="doForceLoan">放贷</NButton>
        </div>

        <h3>免除债务</h3>
        <div class="row">
          <NInputNumber
            v-model:value="forgiveAmount"
            placeholder="金额（>0）"
            :min="0.000001"
            :precision="2"
            :max="selectedUser ? Number(selectedUser.debt) : undefined"
            style="width: 180px"
          />
          <NInput v-model:value="forgiveReason" placeholder="原因（必填）" style="flex: 1; max-width: 320px" />
          <NButton :disabled="!targetUserId" @click="doForgiveDebt">免债</NButton>
        </div>
        <p class="hint">
          ⓘ 免债金额超过当前负债时，自动按当前负债扣减（不会扣到负数）。所有变更后端会做防御性兜底校验。
        </p>
      </section>
    </NSpin>
  </div>
</template>

<style scoped>
.admin-site-config {
  padding: 16px;
}
.panel {
  margin-bottom: 16px;
  border: 2px solid #000;
  padding: 16px;
  background: #fff;
}
.row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin: 8px 0;
  flex-wrap: wrap;
}
.lbl {
  font-weight: 600;
}
.ts {
  font-size: 12px;
  color: #666;
  white-space: nowrap;
}
h2, h3 {
  margin: 0 0 8px 0;
}
code {
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 13px;
}
.user-snapshot {
  margin: 8px 0;
  padding: 8px 12px;
  background: #f5f5f5;
  border: 1px solid #ccc;
  font-size: 13px;
}
.hint {
  font-size: 12px;
  color: #666;
  margin-top: 8px;
}
</style>
