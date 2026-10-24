<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NButton, NInput, NSelect, useDialog, useMessage } from 'naive-ui'
import { adminUsersApi, type AccountBatchAction, type AccountBatchResult } from '@/api/admin'
import { extractErrorMessage } from '@/utils/errors'
const props = defineProps<{ userIds: number[] }>()
const emit = defineEmits<{ changed: []; busy: [value: boolean] }>()
const dialog = useDialog()
const message = useMessage()
const action = ref<AccountBatchAction>('ban')
const reason = ref('')
const busy = ref(false)
const preview = ref<AccountBatchResult | null>(null)
const result = ref<AccountBatchResult | null>(null)
const error = ref('')
const options = [{ label: '封禁', value: 'ban' }, { label: '解封', value: 'unban' }, { label: '平仓', value: 'close' }, { label: '封禁并平仓', value: 'ban_and_close' }]
const readyIds = computed(() => preview.value?.items.filter(i => i.status === 'ready').map(i => i.user_id) ?? [])
const shown = computed(() => result.value ?? preview.value)
const statusLabels = { ready: '可执行', success: '成功', failed: '失败', skipped: '跳过' }
watch([action, reason, () => props.userIds.join(',')], () => { preview.value = null })
watch(busy, value => emit('busy', value))
async function run(dryRun: boolean) {
  if (busy.value || !reason.value.trim() || !props.userIds.length) return
  const ids = dryRun ? [...props.userIds] : [...readyIds.value]
  if (!ids.length) return
  busy.value = true
  error.value = ''
  if (dryRun) { preview.value = null; result.value = null }
  try {
    const response = await adminUsersApi.batchAccounts({ user_ids: ids, action: action.value, reason: reason.value.trim(), dry_run: dryRun })
    if (dryRun) preview.value = response
    else {
      const excluded = preview.value?.items.filter(i => i.status !== 'ready') ?? []
      result.value = { ...response, items: [...excluded, ...response.items] }
      preview.value = null
      if (result.value.items.some(i => i.status === 'failed' || i.status === 'skipped')) message.warning('操作已结束，请查看逐人结果')
      else message.success('操作完成')
      emit('changed')
    }
  } catch (e) { error.value = extractErrorMessage(e, dryRun ? '预览失败' : '执行失败') }
  finally { busy.value = false }
}
function confirmExecute() {
  dialog.warning({ title: '确认账户操作', content: `确认对预览中可执行的 ${readyIds.value.length} 人${options.find(o => o.value === action.value)?.label}？平仓按执行时价格结算，请核对原因和明细。`, positiveText: '确认执行', negativeText: '取消', onPositiveClick: () => run(false) })
}
</script>
<template>
  <div class="batch-account">
    <p>对勾选用户执行账户操作，不处理自己、管理员和官方机器人。平仓会取消待执行订单，不会自动补足现金或免除债务。</p>
    <div class="controls">
      <NSelect v-model:value="action" :options="options" :disabled="busy" style="width:180px" />
      <NInput v-model:value="reason" placeholder="操作原因（必填）" :disabled="busy" :maxlength="500" style="flex:1;min-width:220px" />
      <NButton :loading="busy" :disabled="busy || !userIds.length || userIds.length > 100 || !reason.trim()" @click="run(true)">预览</NButton>
      <NButton type="error" :disabled="busy || !readyIds.length" @click="confirmExecute">确认执行 {{ readyIds.length }} 人</NButton>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <template v-if="shown">
      <h3>{{ shown.dry_run ? '操作预览' : '执行结果' }}</h3>
      <div class="table-scroll"><table>
        <thead><tr><th>用户</th><th>状态</th><th>说明</th><th>现金变化</th><th>交易明细</th></tr></thead>
        <tbody><tr v-for="i in shown.items" :key="i.user_id">
          <td>{{ i.username }} #{{ i.user_id }}</td><td>{{ statusLabels[i.status] }}</td><td>{{ i.message }}</td>
          <td>{{ i.cash_before?.toFixed(2) ?? '—' }} → {{ i.cash_after?.toFixed(2) ?? '—' }}</td>
          <td><div v-for="(t, index) in i.trades" :key="index">{{ t.product === 'fx' ? '外汇' : '预测市场' }} #{{ t.group_id }} {{ ({ sell: '卖出', buy: '买入', cover: '平空', short: '做空' } as Record<string, string>)[t.side] ?? t.side }} {{ t.quantity.toFixed(4) }}，金 {{ t.gold.toFixed(2) }}</div></td>
        </tr></tbody>
      </table></div>
      <h3 v-if="shown.price_impacts.length">依次平仓后的价格变化</h3>
      <p v-for="p in shown.price_impacts" :key="p.pair_id">{{ p.currency_name }}：{{ p.price_before.toFixed(4) }} → {{ p.price_after.toFixed(4) }}</p>
    </template>
  </div>
</template>
<style scoped>
.controls{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}p{font-size:13px;color:#555}h3{font-weight:700;margin:16px 0 8px}.table-scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:10px;border-bottom:1px solid #ccc;vertical-align:top}.error{color:#b91c1c}
</style>
