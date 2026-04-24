<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  NTable, NInput, NButton, NSpin, NAlert, useMessage,
  NInputNumber, NDivider,
} from 'naive-ui'
import { adminSiteConfigApi, type SiteConfigItem } from '@/api/loan'

const configs = ref<SiteConfigItem[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const drafts = ref<Record<string, string>>({})
const msg = useMessage()

// 强制放贷 / 免债表单
const targetUserId = ref<number | null>(null)
const forceAmount = ref<string>('')
const forceReason = ref<string>('')
const forgiveAmount = ref<string>('')
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

async function doForceLoan() {
  if (!targetUserId.value) return msg.error('请输入用户 ID')
  if (!forceAmount.value) return msg.error('请输入金额')
  if (!forceReason.value) return msg.error('请填写原因')
  try {
    await adminSiteConfigApi.forceLoan(targetUserId.value, forceAmount.value, forceReason.value)
    msg.success('放贷成功')
    forceAmount.value = ''
    forceReason.value = ''
  } catch (e: any) {
    msg.error(e?.message ?? '失败')
  }
}

async function doForgiveDebt() {
  if (!targetUserId.value) return msg.error('请输入用户 ID')
  if (!forgiveAmount.value) return msg.error('请输入金额')
  if (!forgiveReason.value) return msg.error('请填写原因')
  try {
    await adminSiteConfigApi.forgiveDebt(targetUserId.value, forgiveAmount.value, forgiveReason.value)
    msg.success('免债成功')
    forgiveAmount.value = ''
    forgiveReason.value = ''
  } catch (e: any) {
    msg.error(e?.message ?? '失败')
  }
}

onMounted(load)
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
          <span class="lbl">目标用户 ID：</span>
          <NInputNumber v-model:value="targetUserId" placeholder="user_id" :min="1" />
        </div>

        <h3>强制放贷（受 loan_enabled 约束）</h3>
        <div class="row">
          <NInput v-model:value="forceAmount" placeholder="金额" />
          <NInput v-model:value="forceReason" placeholder="原因" />
          <NButton type="warning" @click="doForceLoan">放贷</NButton>
        </div>

        <h3>免除债务</h3>
        <div class="row">
          <NInput v-model:value="forgiveAmount" placeholder="金额" />
          <NInput v-model:value="forgiveReason" placeholder="原因" />
          <NButton @click="doForgiveDebt">免债</NButton>
        </div>
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
</style>
