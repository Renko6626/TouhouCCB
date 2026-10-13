<script setup lang="ts">
import { computed, onMounted, ref, provide } from 'vue'
import { useMessage } from 'naive-ui'
import { fxAdminApi, mapFxError } from '@/api/fx'
const config = ref<Record<string, string> | null>(null)
const loading = ref(true), busy = ref(false), error = ref('')
const shortDraft = ref('')
const msg = useMessage()
const gate = computed(() => config.value?.fx_enabled === undefined ? null : config.value.fx_enabled.toLowerCase() === 'true')
provide('fxAdminGate', gate)
async function loadConfig() {
  loading.value = true; error.value = ''; config.value = null
  try { config.value = await fxAdminApi.getConfig(); shortDraft.value = config.value.fx_short_enabled ?? '' }
  catch (e) { error.value = mapFxError(e, '交易开关读取失败') }
  finally { loading.value = false }
}
async function save(key: string, value: string) {
  if (busy.value || !config.value) return
  busy.value = true
  try { config.value = { ...config.value, ...await fxAdminApi.setConfig(key, value) }; msg.success('交易开关已更新') }
  catch (e) { msg.error(mapFxError(e, '交易开关更新失败')) }
  finally { busy.value = false }
}
onMounted(loadConfig)
</script>
<template>
  <div class="fx-admin fx-framework">
    <h1 class="page-title">FX 管理</h1>
    <section class="fx-gate">
      <div v-if="loading">交易开关读取中…</div>
      <div v-else-if="error" class="fx-error" role="alert">{{ error }} <button class="btn-secondary" @click="loadConfig">重新读取</button></div>
      <template v-else>
        <div><strong>交易总闸：{{ gate === null ? '状态未知' : gate ? '已开启' : '已关闭' }}</strong><p>关闭总闸会停止玩家买卖，仍可查看行情与管理市场资金。</p></div>
        <button class="btn-primary" :disabled="busy || gate === null" @click="save('fx_enabled', gate ? 'false' : 'true')">{{ gate ? '关闭总闸' : '开启总闸' }}</button>
        <label>新增开空 <select v-model="shortDraft" class="fx-input" :disabled="busy"><option disabled value="">状态未知</option><option value="false">关闭</option><option value="true">允许（仍受额度与风控限制）</option></select></label>
        <button class="btn-secondary" :disabled="busy || shortDraft === ''" @click="save('fx_short_enabled', shortDraft)">保存开空设置</button>
      </template>
    </section>
    <nav class="fx-admin-tabs" aria-label="FX 管理页面"><RouterLink to="/admin/fx/markets">市场登记</RouterLink><RouterLink to="/admin/fx/operations">央行操作</RouterLink></nav>
    <RouterView />
  </div>
</template>
<style>
.fx-framework.fx-admin {
  padding: 16px;
  max-width: 1200px;
  margin: 0 auto;
}
.fx-framework .page-title {
  font-size: 22px;
  font-weight: 700;
  margin: 0 0 16px;
}
.fx-framework .fx-state {
  border: 2px solid #000;
  padding: 24px;
  background: #fff;
  font-weight: 600;
  display: flex;
  gap: 12px;
  align-items: center;
}
.fx-framework .fx-state-error {
  color: var(--color-down, #dc2626);
}
.fx-framework .fx-gate {
  border: 2px solid #000;
  background: #fff;
  padding: 14px 16px;
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.fx-framework .fx-gate.on {
  background: #fffbeb;
  border-color: #b45309;
}
.fx-framework .fx-gate p {
  margin: 4px 0 0;
  font-size: 12px;
  color: #555;
}
.fx-framework .fx-panel {
  border: 2px solid #000;
  background: #fff;
  padding: 16px;
  margin-bottom: 16px;
}
.fx-framework .fx-panel h2 {
  margin: 0 0 10px;
  font-size: 15px;
  font-weight: 800;
}
.fx-framework .fx-panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.fx-framework .fx-gap-note {
  border-left: 3px solid #b45309;
  background: #fffbeb;
  padding: 8px 10px;
  font-size: 12px;
  line-height: 1.6;
  color: #92400e;
  margin: 8px 0;
}
.fx-framework .fx-config-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 18px;
}
.fx-framework .fx-config-item > span {
  display: block;
  font-size: 12px;
  color: #444;
  margin-bottom: 3px;
}
.fx-framework .fx-config-description {
  display: block;
  color: #555;
  font-size: 12px;
  line-height: 1.45;
}
.fx-framework .fx-config-item code {
  color: #999;
  margin-left: 6px;
  font-size: 11px;
}
.fx-framework .fx-config-row {
  display: flex;
  gap: 6px;
  align-items: center;
}
.fx-framework .fx-input {
  border: 2px solid #000;
  padding: 6px 8px;
  font-family: inherit;
  font-size: 13px;
  background: #fff;
  width: 100%;
}
.fx-framework .fx-input:disabled {
  background: #f0f0f0;
  color: #777;
}
.fx-framework .fx-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin-top: 16px;
}
.fx-framework .fx-form-card {
  border: 1.5px solid #000;
  padding: 12px;
  background: #fafafa;
}
.fx-framework .fx-edit-panel {
  margin-top: 16px;
}
.fx-framework .fx-form-card h3 {
  margin: 0 0 10px;
  font-size: 14px;
}
.fx-framework .fx-pair-setup {
  grid-column: 1 / -1;
}
.fx-framework .fx-setup-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  border-bottom: 2px solid #000;
  margin-bottom: 12px;
}
.fx-framework .fx-setup-badge {
  font-size: 12px;
  font-weight: 700;
}
.fx-framework .fx-setup-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 24px;
}
.fx-framework .fx-setup-fields small {
  color: #555;
  font-weight: 400;
}
.fx-framework .fx-setup-preview {
  background: #f5f5f5;
  border: 1px solid #bbb;
  padding: 16px;
  margin: 16px 0;
  overflow-wrap: anywhere;
}
.fx-framework .fx-setup-preview h4 {
  margin: 0 0 12px;
}
.fx-framework .fx-setup-preview dl {
  margin: 0;
  font-size: 13px;
}
.fx-framework .fx-setup-preview dl > div {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 4px 16px;
  padding: 8px 0;
  border-bottom: 1px solid #ddd;
}
.fx-framework .fx-setup-preview dd {
  margin: 0;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.fx-framework .fx-form-card label {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 12px;
  color: #444;
  margin-bottom: 8px;
}
.fx-framework .fx-form-actions {
  display: flex;
  gap: 8px;
}
.fx-framework .fx-actions-cell {
  min-width: 160px;
}
.fx-framework .fx-error {
  color: var(--color-down, #dc2626);
  font-size: 12px;
  font-weight: 600;
  margin: 4px 0;
}
.fx-framework .fx-hint {
  font-size: 12px;
  color: #777;
  line-height: 1.5;
  margin: 8px 0 0;
}
.fx-framework .table-wrap {
  margin-top: 8px;
  overflow-x: auto;
  border: 2px solid #000;
}
.fx-framework .fx-table {
  width: 100%;
  border-collapse: collapse;
  background: #fff;
}
.fx-framework .fx-table th,
.fx-framework .fx-table td {
  border: 1px solid #ddd;
  padding: 6px 10px;
  text-align: left;
  white-space: nowrap;
  font-size: 12px;
}
.fx-framework .fx-table th {
  background: #000;
  color: #fff;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-size: 11px;
}
.fx-framework .fx-empty-cell {
  text-align: center;
  color: #888;
}
@media (max-width: 900px) {
  .fx-framework .fx-setup-fields {
    grid-template-columns: 1fr;
  }
  .fx-framework .fx-config-grid,
  .fx-framework .fx-form-grid {
    grid-template-columns: 1fr;
  }
}
</style>

<style>
.fx-framework > .fx-admin { padding: 0; }
.fx-framework .fx-admin-tabs { display:flex; gap:0; margin-bottom:16px; border-bottom:2px solid #000; }
.fx-framework .fx-admin-tabs a { padding:10px 18px; color:#000; font-weight:700; }
.fx-framework .fx-admin-tabs .router-link-active { background:#000; color:#fff; }
.fx-framework button:disabled { opacity:.5; cursor:not-allowed; }
.fx-framework input:focus-visible,
.fx-framework select:focus-visible,
.fx-framework button:focus-visible,
.fx-framework a:focus-visible { outline:3px solid #777; outline-offset:3px; }
</style>
