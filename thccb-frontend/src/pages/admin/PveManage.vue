<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  NAlert, NButton, NInput, NInputNumber, NModal, NSelect, NSpin, NSwitch,
  NTable, NTag, useDialog, useMessage, type SelectOption,
} from 'naive-ui'
import {
  pveApi,
  type PveBotItem, type PveConfigEntry, type PveLogEntry, type PveOverview,
} from '@/api/pve'

const msg = useMessage()
const dialog = useDialog()

const overview = ref<PveOverview | null>(null)
const bots = ref<PveBotItem[]>([])
const config = ref<Record<string, PveConfigEntry>>({})
const loading = ref(false)
const error = ref<string | null>(null)

function errDetail(e: unknown): string {
  return (e as { data?: { detail?: string }; message?: string })?.data?.detail
    ?? (e as { message?: string })?.message ?? '操作失败'
}

async function refresh() {
  loading.value = true
  error.value = null
  try {
    const [ov, list, cfg] = await Promise.all([
      pveApi.overview(), pveApi.listBots(), pveApi.getConfig(),
    ])
    overview.value = ov
    bots.value = list
    config.value = cfg
    configDraft.value = Object.fromEntries(
      Object.entries(cfg).map(([k, v]) => [k, v.value]),
    )
  } catch (e: unknown) {
    error.value = errDetail(e)
  } finally {
    loading.value = false
  }
}

onMounted(refresh)

function formatTs(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}

const templateOptions = computed<SelectOption[]>(() =>
  (overview.value?.templates ?? []).map(t => ({ label: t, value: t })),
)

// ── 急停闸 ──────────────────────────────────────────────────────────────

const pveEnabled = computed(() => config.value['pve_enabled']?.value === 'true')

async function toggleEnabled(on: boolean) {
  const doIt = async () => {
    try {
      await pveApi.putConfig({ pve_enabled: on ? 'true' : 'false' })
      msg.success(on ? 'PvE 已启动' : 'PvE 已急停（最迟一个 tick 周期内全体停手）')
      await refresh()
    } catch (e: unknown) {
      msg.error(errDetail(e))
    }
  }
  if (on) {
    dialog.warning({
      title: '启动 PvE 引擎',
      content: '所有 active 机器人将进入调度并真实下单交易。确认？',
      positiveText: '启动',
      negativeText: '取消',
      onPositiveClick: doIt,
    })
  } else {
    await doIt()
  }
}

// ── 批量生成 ────────────────────────────────────────────────────────────

const genTemplate = ref<string | null>(null)
const genCount = ref<number>(5)
const genStyle = ref<'npc' | 'lowkey'>('lowkey')
const genCash = ref<number>(200)
const genScope = ref('')
const generating = ref(false)
const styleOptions: SelectOption[] = [
  { label: '低调款（混在人群里）', value: 'lowkey' },
  { label: '辨识度款（NPC·xx）', value: 'npc' },
]

function parseScope(raw: string): number[] | null {
  const ids = raw.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean).map(Number)
  if (ids.some(n => !Number.isInteger(n) || n <= 0)) return null
  return ids.length ? ids : null
}

async function generate() {
  if (!genTemplate.value) { msg.warning('选择模板'); return }
  const scope = parseScope(genScope.value)
  if (genScope.value.trim() && scope === null) { msg.warning('市场范围格式：逗号分隔的市场 id'); return }
  generating.value = true
  try {
    const r = await pveApi.generate({
      items: [{ template: genTemplate.value, count: genCount.value }],
      naming_style: genStyle.value,
      initial_cash: String(genCash.value),
      market_scope: scope,
    })
    msg.success(`已生成 ${r.created.length} 个机器人：${r.created.map(c => c.username).join('、')}`)
    await refresh()
  } catch (e: unknown) {
    msg.error(errDetail(e))
  } finally {
    generating.value = false
  }
}

// ── 个体操作 ────────────────────────────────────────────────────────────

async function togglePause(b: PveBotItem) {
  try {
    await pveApi.patchBot(b.profile_id, { status: b.status === 'paused' ? 'active' : 'paused' })
    msg.success(b.status === 'paused' ? `已恢复 ${b.username}` : `已暂停 ${b.username}`)
    await refresh()
  } catch (e: unknown) {
    msg.error(errDetail(e))
  }
}

// 注资（dead 顺带复活）
const fundTarget = ref<PveBotItem | null>(null)
const fundAmount = ref<number>(100)
const fundReason = ref('')

async function submitFund() {
  if (!fundTarget.value) return
  try {
    const r = await pveApi.fund(fundTarget.value.profile_id, String(fundAmount.value), fundReason.value || undefined)
    msg.success(`已注资 ${fundTarget.value.username}，现金 ${r.new_cash.toFixed(2)}（状态 ${r.status}）`)
    fundTarget.value = null
    await refresh()
  } catch (e: unknown) {
    msg.error(errDetail(e))
  }
}

// 参数编辑（params JSON + 模板 + 市场范围）
const editTarget = ref<PveBotItem | null>(null)
const editParamsText = ref('')
const editTemplate = ref('')
const editScope = ref('')

function openEdit(b: PveBotItem) {
  editTarget.value = b
  editParamsText.value = JSON.stringify(b.params, null, 2)
  editTemplate.value = b.template
  editScope.value = (b.market_scope ?? []).join(',')
}

async function submitEdit() {
  if (!editTarget.value) return
  let params: Record<string, unknown>
  try {
    params = JSON.parse(editParamsText.value)
  } catch {
    msg.error('params 不是合法 JSON')
    return
  }
  const scope = parseScope(editScope.value)
  if (editScope.value.trim() && scope === null) { msg.error('市场范围格式：逗号分隔的市场 id'); return }
  try {
    await pveApi.patchBot(editTarget.value.profile_id, {
      params,
      template: editTemplate.value,
      market_scope: scope,
    })
    msg.success(`已更新 ${editTarget.value.username}`)
    editTarget.value = null
    await refresh()
  } catch (e: unknown) {
    msg.error(errDetail(e))
  }
}

// 决策日志
const logTarget = ref<PveBotItem | null>(null)
const logEntries = ref<PveLogEntry[]>([])

async function openLog(b: PveBotItem) {
  logTarget.value = b
  logEntries.value = []
  try {
    const r = await pveApi.log(b.profile_id)
    logEntries.value = r.log
  } catch (e: unknown) {
    msg.error(errDetail(e))
  }
}

const logTagType = (event: string) => {
  if (event === 'trade') return 'success'
  if (event === 'error') return 'error'
  if (event === 'alert') return 'warning'
  if (event === 'lifecycle') return 'info'
  return 'default'
}

// ── 全局配置 ────────────────────────────────────────────────────────────

const configDraft = ref<Record<string, string>>({})
const savingConfig = ref(false)
// pve_enabled 走顶部开关；其余键在配置面板编辑
const configKeys = computed(() => Object.keys(config.value).filter(k => k !== 'pve_enabled'))

async function saveConfig() {
  const changed: Record<string, string> = {}
  for (const k of configKeys.value) {
    const draft = configDraft.value[k]
    if (draft !== undefined && draft !== config.value[k]?.value) changed[k] = draft
  }
  if (!Object.keys(changed).length) { msg.info('没有改动'); return }
  savingConfig.value = true
  try {
    await pveApi.putConfig(changed)
    msg.success(`已保存：${Object.keys(changed).join('、')}`)
    await refresh()
  } catch (e: unknown) {
    msg.error(errDetail(e))
  } finally {
    savingConfig.value = false
  }
}

const statusTagType = (s: string) => (s === 'active' ? 'success' : s === 'paused' ? 'warning' : 'error')
const statusLabel = (s: string) => (s === 'active' ? '在编' : s === 'paused' ? '已暂停' : '已死亡')
</script>

<template>
  <div class="pve-manage">
    <NSpin :show="loading">
      <NAlert v-if="error" type="error" :title="error" />

      <!-- 总览 + 急停闸 -->
      <section class="panel stats-panel">
        <div class="stats-row">
          <div class="stat-item">
            <span class="stat-label">引擎</span>
            <NSwitch :value="pveEnabled" @update:value="toggleEnabled">
              <template #checked>运行中</template>
              <template #unchecked>已停止</template>
            </NSwitch>
          </div>
          <div class="stat-item">
            <span class="stat-label">在编 / 暂停 / 死亡</span>
            <span class="stat-value">
              {{ overview?.counts.active ?? '—' }} /
              {{ overview?.counts.paused ?? '—' }} /
              <span class="danger">{{ overview?.counts.dead ?? '—' }}</span>
            </span>
          </div>
          <div class="stat-item">
            <span class="stat-label">调度中</span>
            <span class="stat-value">{{ overview?.engine.scheduled_bots ?? '—' }}</span>
          </div>
          <div class="stat-item">
            <span class="stat-label">近 1 分钟下单</span>
            <span class="stat-value">{{ overview?.engine.orders_last_min ?? '—' }}</span>
          </div>
          <NButton size="small" @click="refresh">刷新</NButton>
        </div>
      </section>

      <!-- 批量生成 -->
      <section class="panel">
        <h2>批量生成</h2>
        <div class="row">
          <NSelect v-model:value="genTemplate" :options="templateOptions" placeholder="模板" size="small" style="width: 140px" />
          <NInputNumber v-model:value="genCount" :min="1" :max="50" size="small" style="width: 100px">
            <template #suffix>个</template>
          </NInputNumber>
          <NSelect v-model:value="genStyle" :options="styleOptions" size="small" style="width: 200px" />
          <NInputNumber v-model:value="genCash" :min="0" :max="100000" size="small" style="width: 140px">
            <template #prefix>注资</template>
          </NInputNumber>
          <NInput v-model:value="genScope" placeholder="市场范围 id（逗号分隔，空=全部）" size="small" style="width: 220px" />
          <NButton type="primary" size="small" :loading="generating" @click="generate">生成</NButton>
        </div>
        <p class="hint">初始注资走 ledger 调账记账；人格参数按模板默认值随机扰动落库，生成后可逐个调整。</p>
      </section>

      <!-- 账户池 -->
      <section class="panel">
        <h2>账户池（{{ bots.length }}）</h2>
        <NTable v-if="bots.length > 0" :bordered="true" :single-line="false" size="small">
          <thead>
            <tr>
              <th>机器人</th>
              <th>模板</th>
              <th>状态</th>
              <th class="num">现金</th>
              <th class="num">持仓(LCV)</th>
              <th class="num">总资产</th>
              <th class="num">今日成交</th>
              <th>范围</th>
              <th>下次行动</th>
              <th>最近成交</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="b in bots" :key="b.profile_id">
              <td>
                <div class="user-cell">
                  <strong>{{ b.username }}</strong>
                  <code class="user-id">uid={{ b.user_id }}</code>
                </div>
              </td>
              <td><NTag size="small">{{ b.template }}</NTag></td>
              <td>
                <NTag :type="statusTagType(b.status)" size="small">{{ statusLabel(b.status) }}</NTag>
                <NTag v-if="b.status === 'active' && !b.scheduled" size="small" type="warning">未入调度</NTag>
              </td>
              <td class="num mono">{{ b.cash.toFixed(2) }}</td>
              <td class="num mono">{{ b.holdings_value.toFixed(2) }}</td>
              <td class="num mono"><strong>{{ b.total_value.toFixed(2) }}</strong></td>
              <td class="num mono">{{ b.today_turnover.toFixed(2) }}</td>
              <td class="mono small">{{ b.market_scope?.join(',') || '全部' }}</td>
              <td class="ts">{{ formatTs(b.next_action_at) }}</td>
              <td class="ts">{{ formatTs(b.last_trade_at) }}</td>
              <td class="action-cell">
                <NButton v-if="b.status !== 'dead'" size="tiny" @click="togglePause(b)">
                  {{ b.status === 'paused' ? '恢复' : '暂停' }}
                </NButton>
                <NButton size="tiny" :type="b.status === 'dead' ? 'error' : 'default'"
                         @click="fundTarget = b; fundAmount = 100; fundReason = ''">
                  {{ b.status === 'dead' ? '复活' : '注资' }}
                </NButton>
                <NButton size="tiny" @click="openEdit(b)">参数</NButton>
                <NButton size="tiny" @click="openLog(b)">日志</NButton>
              </td>
            </tr>
          </tbody>
        </NTable>
        <div v-else class="empty-hint">账户池为空——用上面的「批量生成」建立编制</div>
      </section>

      <!-- 全局配置 -->
      <section class="panel">
        <div class="section-head">
          <h2>全局配置</h2>
          <NButton size="small" type="primary" :loading="savingConfig" @click="saveConfig">保存改动</NButton>
        </div>
        <NTable :bordered="true" :single-line="false" size="small">
          <thead>
            <tr><th>键</th><th>类型</th><th style="width: 200px">值</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="k in configKeys" :key="k">
              <td class="mono">{{ k }}</td>
              <td class="small">{{ config[k]?.value_type }}</td>
              <td>
                <NInput :value="configDraft[k] ?? ''" size="small"
                        @update:value="(v: string) => { configDraft[k] = v }" />
              </td>
              <td>
                <NTag v-if="config[k]?.is_default" size="small">默认值</NTag>
                <NTag v-else size="small" type="info">已落库</NTag>
              </td>
            </tr>
          </tbody>
        </NTable>
        <p class="hint">
          pve_tick_interval_sec 改后需重启后端生效；其余热生效。
          leaderboard/wealth_stats_include_bots 控制排行榜与财富统计是否计入机器人。
        </p>
      </section>
    </NSpin>

    <!-- 注资 modal -->
    <NModal :show="fundTarget !== null" preset="card" :title="`注资：${fundTarget?.username ?? ''}`"
            style="width: 420px" @update:show="(v: boolean) => { if (!v) fundTarget = null }">
      <div class="modal-form">
        <NInputNumber v-model:value="fundAmount" :min="0.01" :max="100000" style="width: 100%">
          <template #prefix>金额</template>
        </NInputNumber>
        <NInput v-model:value="fundReason" placeholder="原因（选填，写入 ledger）" />
        <p class="hint" v-if="fundTarget?.status === 'dead'">该机器人已死亡，注资后自动复活并重新进入调度。</p>
        <div class="row right">
          <NButton size="small" @click="fundTarget = null">取消</NButton>
          <NButton size="small" type="primary" @click="submitFund">确认注资</NButton>
        </div>
      </div>
    </NModal>

    <!-- 参数编辑 modal -->
    <NModal :show="editTarget !== null" preset="card" :title="`参数：${editTarget?.username ?? ''}`"
            style="width: 560px" @update:show="(v: boolean) => { if (!v) editTarget = null }">
      <div class="modal-form">
        <div class="row">
          <span class="field-label">模板</span>
          <NSelect v-model:value="editTemplate" :options="templateOptions" size="small" style="width: 160px" />
          <span class="field-label">市场范围</span>
          <NInput v-model:value="editScope" placeholder="空=全部" size="small" style="width: 160px" />
        </div>
        <NInput v-model:value="editParamsText" type="textarea" :rows="14" class="mono" />
        <p class="hint">params 为 JSON；换模板会清空该机器人的进程内记忆（网格线/主场等）。</p>
        <div class="row right">
          <NButton size="small" @click="editTarget = null">取消</NButton>
          <NButton size="small" type="primary" @click="submitEdit">保存</NButton>
        </div>
      </div>
    </NModal>

    <!-- 决策日志 modal -->
    <NModal :show="logTarget !== null" preset="card" :title="`决策流水：${logTarget?.username ?? ''}`"
            style="width: 640px" @update:show="(v: boolean) => { if (!v) logTarget = null }">
      <NTable v-if="logEntries.length > 0" :bordered="true" :single-line="false" size="small">
        <thead>
          <tr><th style="width: 160px">时间</th><th style="width: 90px">事件</th><th>内容</th></tr>
        </thead>
        <tbody>
          <tr v-for="(e, i) in logEntries" :key="i">
            <td class="ts">{{ formatTs(e.ts) }}</td>
            <td><NTag :type="logTagType(e.event)" size="small">{{ e.event }}</NTag></td>
            <td class="small">{{ e.msg }}</td>
          </tr>
        </tbody>
      </NTable>
      <div v-else class="empty-hint">暂无决策记录（内存环形缓冲，重启后清空）</div>
    </NModal>
  </div>
</template>

<style scoped>
.pve-manage {
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
  gap: 4px;
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
.danger {
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
.num {
  text-align: right;
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
.row.right {
  justify-content: flex-end;
}
.field-label {
  font-size: 12px;
  font-weight: 700;
}
.modal-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.hint {
  font-size: 12px;
  color: #666;
  margin: 0 0 6px 0;
}
</style>
