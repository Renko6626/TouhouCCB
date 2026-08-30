<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  NAlert, NButton, NInput, NInputNumber, NModal, NSelect, NSpin, NSwitch,
  NTable, NTag, useDialog, useMessage, type SelectOption,
} from 'naive-ui'
import {
  pveApi,
  type PveBotItem, type PveConfigEntry, type PveLogEntry, type PveOverview,
  type PveTemplateDetail,
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

// ── 模板图鉴（后端 docstring 是解说的单一来源，新模板自动出现在这里）────────

const templateDetails = computed<PveTemplateDetail[]>(() => {
  const details = overview.value?.template_details ?? []
  // 量化底盘在前、散户人格在后，同组按中文名稳定排序
  return [...details].sort((a, b) =>
    a.group === b.group ? a.title.localeCompare(b.title, 'zh') : a.group === 'quant' ? -1 : 1)
})
const detailByName = computed<Record<string, PveTemplateDetail>>(() =>
  Object.fromEntries(templateDetails.value.map(d => [d.name, d])))
const templateTitle = (name: string) => detailByName.value[name]?.title ?? name
const groupLabel = (g: string) => (g === 'quant' ? '量化底盘' : '散户人格')
// docstring 展示：去掉与 summary 重复的首行，按空行分段，段内源码换行合并
function personaParagraphs(d: PveTemplateDetail): string[] {
  const rest = d.description.startsWith(d.summary)
    ? d.description.slice(d.summary.length)
    : d.description
  return rest
    .split(/\n\s*\n/)
    .map(p => p.replace(/\s*\n\s*/g, '').trim())
    .filter(Boolean)
}
const paramDocs = computed<Record<string, string>>(() => overview.value?.param_docs ?? {})

const templateOptions = computed<SelectOption[]>(() =>
  templateDetails.value.length
    ? templateDetails.value.map(d => ({ label: `${d.title}（${d.name}）`, value: d.name }))
    : (overview.value?.templates ?? []).map(t => ({ label: t, value: t })),
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
const genStyle = ref<'npc' | 'lowkey' | 'phrase'>('lowkey')
const genCash = ref<number>(200)
const genScope = ref('')
const generating = ref(false)
const styleOptions: SelectOption[] = [
  { label: '低调款（混在人群里）', value: 'lowkey' },
  { label: '辨识度款（NPC·xx）', value: 'npc' },
  { label: '句式款（贪婪的散户悄悄抄底）', value: 'phrase' },
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

// 改名（手动指定 or 按词库风格重抽）
const renameTarget = ref<PveBotItem | null>(null)
const renameName = ref('')

async function submitRename(style?: 'npc' | 'lowkey' | 'phrase') {
  if (!renameTarget.value) return
  const payload = style ? { rename_style: style } : { username: renameName.value.trim() }
  if (!style && (payload.username!.length < 2 || payload.username!.length > 32)) {
    msg.error('用户名长度需在 2~32 之间')
    return
  }
  try {
    await pveApi.patchBot(renameTarget.value.profile_id, payload)
    msg.success(style ? '已按词库重新起名' : `已改名为 ${payload.username}`)
    renameTarget.value = null
    await refresh()
  } catch (e: unknown) {
    msg.error(errDetail(e))
  }
}

// 销毁：后端判定走「真删」还是「清算退休」，确认框先把判定结果讲清楚
const destroyTarget = ref<PveBotItem | null>(null)
const destroying = ref(false)
// 有持仓或有过成交 → 一定走退休；两者都没有才可能真删
const destroyWillDelete = computed(
  () => destroyTarget.value !== null && destroyTarget.value.holdings_value <= 0
    && destroyTarget.value.last_trade_at === null,
)

async function submitDestroy() {
  if (!destroyTarget.value) return
  destroying.value = true
  try {
    const r = await pveApi.destroy(destroyTarget.value.profile_id)
    msg.success(r.mode === 'deleted'
      ? `已彻底删除 ${r.username}（从未交易过）`
      : `已清算退役 ${r.username}：平仓 ${r.sold.length} 笔，回收现金 ${r.recovered_cash.toFixed(2)}`)
    destroyTarget.value = null
    await refresh()
  } catch (e: unknown) {
    msg.error(errDetail(e))
  } finally {
    destroying.value = false
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

// 参数编辑：表单模式（带说明的逐项输入）为主，JSON 模式兜底
const editTarget = ref<PveBotItem | null>(null)
const editMode = ref<'form' | 'json'>('form')
const editParams = ref<Record<string, unknown>>({})
const editParamsText = ref('')
const editTemplate = ref('')
const editScope = ref('')

function openEdit(b: PveBotItem) {
  editTarget.value = b
  editParams.value = JSON.parse(JSON.stringify(b.params))
  editParamsText.value = JSON.stringify(b.params, null, 2)
  editTemplate.value = b.template
  editScope.value = (b.market_scope ?? []).join(',')
  editMode.value = 'form'
}

function switchEditMode(mode: 'form' | 'json') {
  if (mode === editMode.value) return
  if (mode === 'json') {
    editParamsText.value = JSON.stringify(editParams.value, null, 2)
  } else {
    try {
      editParams.value = JSON.parse(editParamsText.value)
    } catch {
      msg.error('params 不是合法 JSON，先修好再切回表单')
      return
    }
  }
  editMode.value = mode
}

// 每个参数按值类型挑输入控件；作息/信号源这类语义枚举给下拉
type ParamKind = 'bool' | 'number' | 'preset' | 'signal' | 'text'
function paramKind(key: string, val: unknown): ParamKind {
  if (typeof val === 'boolean') return 'bool'
  if (key === 'active_preset') return 'preset'
  if (key === 'herd_signal') return 'signal'
  if (typeof val === 'number' || val === null) return 'number'
  return 'text'
}
const editParamRows = computed(() =>
  Object.keys(editParams.value).map(k => ({ key: k, kind: paramKind(k, editParams.value[k]) })))
const setParam = (k: string, v: unknown) => { editParams.value[k] = v }
const numParam = (k: string): number | null => {
  const v = editParams.value[k]
  return typeof v === 'number' ? v : null
}
const strParam = (k: string): string => {
  const v = editParams.value[k]
  return typeof v === 'string' ? v : ''
}
const boolParam = (k: string): boolean => editParams.value[k] === true

const PRESET_LABELS: Record<string, string> = {
  always: 'always · 全天候（量化）', worker: 'worker · 上班族', evening: 'evening · 晚间党',
  owl: 'owl · 夜猫', loose: 'loose · 松散',
}
const presetOptions = computed<SelectOption[]>(() =>
  (overview.value?.active_presets ?? []).map(p => ({ label: PRESET_LABELS[p] ?? p, value: p })))
const signalOptions: SelectOption[] = [
  { label: 'price · 跟价格信（图表党）', value: 'price' },
  { label: 'flow · 跟人群信（从众党）', value: 'flow' },
]

async function submitEdit() {
  if (!editTarget.value) return
  let params: Record<string, unknown>
  if (editMode.value === 'form') {
    params = editParams.value
  } else {
    try {
      params = JSON.parse(editParamsText.value)
    } catch {
      msg.error('params 不是合法 JSON')
      return
    }
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

const statusTagType = (s: string) =>
  s === 'active' ? 'success' : s === 'paused' ? 'warning' : s === 'retired' ? 'default' : 'error'
const statusLabel = (s: string) =>
  s === 'active' ? '在编' : s === 'paused' ? '已暂停' : s === 'retired' ? '已退役' : '已死亡'
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

      <!-- 模板图鉴 -->
      <section class="panel">
        <h2>人格图鉴</h2>
        <p class="hint">
          量化底盘全天候提供流动性；散户人格是同一个「信念模型」的不同参数点位——
          行为从信念演化里涌现，没有固定套路可被玩家试探。点开卡片看完整解说。
        </p>
        <div class="persona-grid">
          <details v-for="d in templateDetails" :key="d.name" class="persona-card">
            <summary class="persona-head">
              <span class="persona-title">{{ d.title }}</span>
              <code class="persona-code">{{ d.name }}</code>
              <NTag size="tiny" :type="d.group === 'quant' ? 'info' : 'default'">
                {{ groupLabel(d.group) }}
              </NTag>
            </summary>
            <p class="persona-summary">{{ d.summary }}</p>
            <p v-for="(para, i) in personaParagraphs(d)" :key="i" class="persona-desc">{{ para }}</p>
          </details>
        </div>
      </section>

      <!-- 批量生成 -->
      <section class="panel">
        <h2>批量生成</h2>
        <div class="row">
          <NSelect v-model:value="genTemplate" :options="templateOptions" placeholder="人格模板" size="small" style="width: 210px" />
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
        <p v-if="genTemplate && detailByName[genTemplate]" class="hint persona-pick">
          {{ detailByName[genTemplate]?.summary }}
        </p>
        <p class="hint">初始注资走 ledger 调账记账；人格参数按模板默认值随机扰动落库（同模板个体天然不同），生成后可逐个调整。</p>
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
              <td>
                <div class="user-cell">
                  <NTag size="small">{{ templateTitle(b.template) }}</NTag>
                  <code class="user-id">{{ b.template }}</code>
                </div>
              </td>
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
                <NButton v-if="b.status !== 'retired'" size="tiny"
                         @click="renameTarget = b; renameName = b.username">改名</NButton>
                <NButton v-if="b.status !== 'retired'" size="tiny" type="error" ghost
                         @click="destroyTarget = b">销毁</NButton>
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
            <tr><th>配置项</th><th style="width: 240px">值</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="k in configKeys" :key="k">
              <td>
                <div class="config-cell">
                  <code class="param-key">{{ k }}</code>
                  <span class="param-doc">{{ config[k]?.label }}</span>
                </div>
              </td>
              <td>
                <NInput :value="configDraft[k] ?? ''" size="small"
                        :type="k === 'pve_sentiment' ? 'textarea' : 'text'"
                        :autosize="k === 'pve_sentiment' ? { minRows: 2, maxRows: 4 } : undefined"
                        :placeholder="k === 'pve_sentiment' ? '空=无风向' : ''"
                        @update:value="(v: string) => { configDraft[k] = v }" />
              </td>
              <td>
                <NTag v-if="config[k]?.is_default" size="small">默认值</NTag>
                <NTag v-else size="small" type="info">已落库</NTag>
              </td>
            </tr>
          </tbody>
        </NTable>
      </section>
    </NSpin>

    <!-- 改名 modal -->
    <NModal :show="renameTarget !== null" preset="card" :title="`改名：${renameTarget?.username ?? ''}`"
            style="width: 460px" @update:show="(v: boolean) => { if (!v) renameTarget = null }">
      <div class="modal-form">
        <NInput v-model:value="renameName" placeholder="新名字（2~32 字）" />
        <div class="row right">
          <NButton size="small" @click="renameTarget = null">取消</NButton>
          <NButton size="small" type="primary" @click="submitRename()">确认改名</NButton>
        </div>
        <p class="hint">或者直接从词库重抽一个（自动避开已占用的名字）：</p>
        <div class="row">
          <NButton size="small" @click="submitRename('npc')">NPC 款</NButton>
          <NButton size="small" @click="submitRename('lowkey')">低调款</NButton>
          <NButton size="small" @click="submitRename('phrase')">句式款</NButton>
        </div>
      </div>
    </NModal>

    <!-- 销毁 modal -->
    <NModal :show="destroyTarget !== null" preset="card" :title="`销毁：${destroyTarget?.username ?? ''}`"
            style="width: 480px" @update:show="(v: boolean) => { if (!v) destroyTarget = null }">
      <div class="modal-form">
        <template v-if="destroyWillDelete">
          <p class="hint danger">
            该机器人从未交易过，将被<strong>彻底删除</strong>——账号、机器人档案、初始注资流水全部清除，不可恢复。
          </p>
        </template>
        <template v-else>
          <p class="hint">
            该机器人有过交易，将走<strong>清算退役</strong>：先按市价强制平掉全部持仓（份额退回市场、价格相应回落），
            回收剩余现金进 ledger，然后永久退出调度。账号与全部成交流水保留，退役后不可恢复。
          </p>
          <p class="hint">当前持仓估值 {{ destroyTarget?.holdings_value.toFixed(2) }} ·
            现金 {{ destroyTarget?.cash.toFixed(2) }}</p>
        </template>
        <div class="row right">
          <NButton size="small" @click="destroyTarget = null">取消</NButton>
          <NButton size="small" type="error" :loading="destroying" @click="submitDestroy">
            {{ destroyWillDelete ? '确认彻底删除' : '确认清算退役' }}
          </NButton>
        </div>
      </div>
    </NModal>

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
    <NModal :show="editTarget !== null" preset="card" :title="`人格调校：${editTarget?.username ?? ''}`"
            style="width: 640px" @update:show="(v: boolean) => { if (!v) editTarget = null }">
      <div class="modal-form">
        <div class="row">
          <span class="field-label">模板</span>
          <NSelect v-model:value="editTemplate" :options="templateOptions" size="small" style="width: 210px" />
          <span class="field-label">市场范围</span>
          <NInput v-model:value="editScope" placeholder="空=全部" size="small" style="width: 140px" />
          <span class="spacer" />
          <NButton size="tiny" :type="editMode === 'form' ? 'primary' : 'default'"
                   @click="switchEditMode('form')">表单</NButton>
          <NButton size="tiny" :type="editMode === 'json' ? 'primary' : 'default'"
                   @click="switchEditMode('json')">JSON</NButton>
        </div>
        <p v-if="editTemplate && detailByName[editTemplate]" class="hint persona-pick">
          {{ detailByName[editTemplate]?.summary }}
        </p>

        <div v-if="editMode === 'form'" class="param-form">
          <div v-for="row in editParamRows" :key="row.key" class="param-row">
            <div class="param-info">
              <code class="param-key">{{ row.key }}</code>
              <span class="param-doc">{{ paramDocs[row.key] ?? '' }}</span>
            </div>
            <div class="param-input">
              <NSwitch v-if="row.kind === 'bool'" size="small" :value="boolParam(row.key)"
                       @update:value="(v: boolean) => setParam(row.key, v)" />
              <NSelect v-else-if="row.kind === 'preset'" size="small" :value="strParam(row.key)"
                       :options="presetOptions" style="width: 190px"
                       @update:value="(v: string) => setParam(row.key, v)" />
              <NSelect v-else-if="row.kind === 'signal'" size="small" :value="strParam(row.key)"
                       :options="signalOptions" style="width: 190px"
                       @update:value="(v: string) => setParam(row.key, v)" />
              <NInputNumber v-else-if="row.kind === 'number'" size="small" :value="numParam(row.key)"
                            clearable placeholder="留空=自动" style="width: 150px" :show-button="false"
                            @update:value="(v: number | null) => setParam(row.key, v)" />
              <NInput v-else size="small" :value="strParam(row.key)" style="width: 190px"
                      @update:value="(v: string) => setParam(row.key, v)" />
            </div>
          </div>
        </div>
        <NInput v-else v-model:value="editParamsText" type="textarea" :rows="14" class="mono" />

        <p class="hint">改动下一个 tick 生效；换模板会清空该机器人的进程内记忆（信念/网格线/主场等），相当于换了个人。</p>
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
.persona-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 10px;
  margin-top: 10px;
}
.persona-card {
  border: 2px solid #000;
  padding: 10px 12px;
  background: #fafafa;
}
.persona-card[open] {
  background: #fff;
}
.persona-head {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  list-style: none;
  user-select: none;
}
.persona-head::-webkit-details-marker {
  display: none;
}
.persona-title {
  font-weight: 800;
  font-size: 14px;
}
.persona-code {
  font-family: monospace;
  font-size: 11px;
  color: #888;
}
.persona-summary {
  margin: 8px 0 0 0;
  font-size: 12px;
  font-weight: 700;
}
.persona-desc {
  margin: 6px 0 0 0;
  font-size: 12px;
  color: #444;
  line-height: 1.7;
}
.persona-summary + .persona-desc {
  border-top: 1.5px dashed #ccc;
  padding-top: 6px;
}
.persona-pick {
  border-left: 3px solid #000;
  padding-left: 8px;
  color: #000;
}
.param-form {
  max-height: 46vh;
  overflow-y: auto;
  border: 1.5px solid #ddd;
}
.param-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 10px;
  border-bottom: 1px solid #eee;
}
.param-row:last-child {
  border-bottom: none;
}
.param-info,
.config-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.param-key {
  font-family: monospace;
  font-size: 12px;
  font-weight: 700;
}
.param-doc {
  font-size: 11px;
  color: #777;
  line-height: 1.5;
}
.param-input {
  flex-shrink: 0;
}
.spacer {
  flex: 1;
}
</style>
