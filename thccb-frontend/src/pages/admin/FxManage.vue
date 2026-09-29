<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { formatFxAmount, fxAdminApi, fxApi, mapFxError, mergeFxPairs } from '@/api/fx'
import type {
  FxEventAdmin,
  FxIntervention,
  FxPairAdmin,
  FxPairAdminDetail,
  FxPairPublic,
  FxPairStatus,
  FxSnapshot,
} from '@/types/fx'

const msg = useMessage()

const loading = ref(true)
const error = ref<string | null>(null)
const config = ref<Record<string, string>>({})
const configDraft = ref<Record<string, string>>({})
const pairs = ref<FxPairPublic[]>([])
// 管理端只读接口 GET /api/v1/admin/fx/pairs 是权威来源：包含草稿与 treasury
// 余额/今日支出，刷新后不再丢失运营状态。写操作后统一重拉该接口。
const adminPairs = ref<Record<number, FxPairAdmin>>({})
const treasuryByPair = ref<Record<number, FxPairAdminDetail>>({})
const snapshots = ref<Record<number, FxSnapshot>>({})
const events = ref<FxEventAdmin[]>([])
const interventions = ref<FxIntervention[]>([])
const interventionPairId = ref<number | null>(null)

// 公开列表过滤 draft；合并本地写操作返回的管理员记录，保证本会话内
// 「创建草稿 → 注资 → 开市」全程可见、可选。公开条目存在时优先（字段更新鲜）。
const visiblePairs = computed<FxPairPublic[]>(() => mergeFxPairs(adminPairs.value, pairs.value))

const CONFIG_LABELS: Record<string, string> = {
  fx_enabled: 'FX 总闸（默认关闭）',
  fx_hourly_sigma: '小时波动 sigma',
  fx_step_max_ratio: '单步最大波动比例',
  fx_noise_interval_sec: '噪声平均间隔（秒）',
  fx_noise_pool_ratio: '噪声单笔池子比例',
  fx_system_half_life_sec: '常规干预半衰期（秒）',
  fx_default_price_move_limit: '单 tick 价格变动上限',
  fx_daily_budget: '每日金圆券预算',
}
const CONFIG_ORDER = Object.keys(CONFIG_LABELS)

const PAIR_STATUS_LABELS: Record<string, string> = {
  draft: '草稿',
  trading: '交易中',
  paused: '暂停',
  closed: '关闭',
}
const EVENT_STATUS_LABELS: Record<string, string> = {
  draft: '草稿',
  scheduled: '已计划',
  published: '进行中',
  cancelled: '已取消',
  completed: '已完成',
}

const gateEnabled = computed(() => (config.value.fx_enabled ?? 'false').toLowerCase() === 'true')

const pairForm = ref({
  currency_code: 'CCB',
  currency_name: '幻想币',
  gold_reserve: '1000',
  foreign_reserve: '1000',
  target_price: '1',
  initial_price: '1',
  target_min: '0.5',
  target_max: '2',
  buy_fee_rate: '0.002',
  sell_fee_rate: '0.002',
  status: 'draft' as FxPairStatus,
})

const fundForm = ref({ pair_id: 0, gold_amount: '0', foreign_amount: '0' })

const eventForm = ref({
  pair_id: 0,
  title: '',
  body: '',
  kind: 'macro',
  shock_ratio: '0.01',
  first_reaction_ratio: '0.25',
  window_sec: 180,
  budget: '100',
  scheduled_at: '',
})

const eventKindOptions = [
  { value: 'macro', label: '普通新闻（冲击 ≤ 5%）' },
  { value: 'black_swan', label: '黑天鹅（冲击 ≤ 20%）' },
]

const createPairError = ref<string | null>(null)
const eventError = ref<string | null>(null)

function toNumber(value: string | number | null | undefined): number {
  const n = Number(value)
  return Number.isFinite(n) ? n : NaN
}

function detailOf(pairId: number | null): FxPairAdmin | null {
  if (pairId === null) return null
  return adminPairs.value[pairId] ?? null
}

function snapshotOf(pairId: number | null): FxSnapshot | null {
  if (pairId === null) return null
  return snapshots.value[pairId] ?? null
}

function treasuryOf(pairId: number | null): FxPairAdminDetail | null {
  if (pairId === null) return null
  return treasuryByPair.value[pairId] ?? null
}

function snapString(snapshot: Record<string, unknown> | null | undefined, key: string): string {
  if (!snapshot) return '—'
  const value = snapshot[key]
  if (value === undefined || value === null) return '—'
  return typeof value === 'string' ? value : String(value)
}

function combinePairStatus(status: FxPairStatus): string {
  return PAIR_STATUS_LABELS[status] ?? status
}

// ── 加载 ──
async function loadConfig() {
  config.value = await fxAdminApi.getConfig()
  configDraft.value = { ...config.value }
}

async function loadAdminPairs() {
  const list = await fxAdminApi.listPairs()
  const controls: Record<number, FxPairAdmin> = {}
  const treasury: Record<number, FxPairAdminDetail> = {}
  for (const p of list) {
    controls[p.id] = p
    treasury[p.id] = p
  }
  adminPairs.value = controls
  treasuryByPair.value = treasury
}

async function loadPairs() {
  const [, publicList] = await Promise.all([loadAdminPairs(), fxApi.listPairs()])
  pairs.value = publicList
  syncDefaultSelections()
}

/** 下拉默认选中第一个可见 pair（含本地草稿）。 */
function syncDefaultSelections() {
  const list = visiblePairs.value
  if (list.length === 0) return
  const firstId = list[0]!.id
  if (fundForm.value.pair_id === 0) fundForm.value.pair_id = firstId
  if (eventForm.value.pair_id === 0) eventForm.value.pair_id = firstId
  if (interventionPairId.value === null) interventionPairId.value = firstId
}

async function loadPublicSnapshots() {
  const entries = await Promise.allSettled(
    visiblePairs.value.map(async (p) => [p.id, await fxApi.getSnapshot(p.id)] as const),
  )
  const next: Record<number, FxSnapshot> = {}
  for (const entry of entries) {
    if (entry.status === 'fulfilled') next[entry.value[0]] = entry.value[1]
  }
  snapshots.value = next
}

async function loadEvents() {
  events.value = await fxAdminApi.listEvents()
}

async function loadAll() {
  loading.value = true
  error.value = null
  try {
    await Promise.all([loadConfig(), loadPairs(), loadEvents()])
    await loadPublicSnapshots()
  } catch (e) {
    error.value = mapFxError(e, 'FX 管理数据加载失败')
  } finally {
    loading.value = false
  }
}

// ── 总闸 ──
async function saveConfigValue(key: string, value: string) {
  try {
    const res = await fxAdminApi.setConfig(key, value)
    config.value = { ...config.value, ...res }
    configDraft.value = { ...configDraft.value, ...res }
    msg.success(`${CONFIG_LABELS[key] ?? key} 已更新`)
  } catch (e) {
    msg.error(mapFxError(e, '配置更新失败'))
  }
}

async function toggleGate() {
  await saveConfigValue('fx_enabled', gateEnabled.value ? 'false' : 'true')
}

// ── 货币对 ──
async function createPair() {
  createPairError.value = null
  const f = pairForm.value
  if (!/^[A-Z0-9_]{1,16}$/.test(f.currency_code)) {
    createPairError.value = '币种代码只能包含大写字母、数字与下划线'
    return
  }
  if (!f.currency_name.trim()) {
    createPairError.value = '请填写币种名称'
    return
  }
  const numeric: Array<[string, number]> = [
    ['金圆券初始储备', toNumber(f.gold_reserve)],
    ['外币初始储备', toNumber(f.foreign_reserve)],
    ['目标价', toNumber(f.target_price)],
    ['初始价', toNumber(f.initial_price)],
    ['目标下限', toNumber(f.target_min)],
    ['目标上限', toNumber(f.target_max)],
  ]
  for (const [label, value] of numeric) {
    if (!Number.isFinite(value) || value <= 0) {
      createPairError.value = `${label}必须是有限正数`
      return
    }
  }
  if (toNumber(f.target_min) > toNumber(f.target_price) || toNumber(f.target_price) > toNumber(f.target_max)) {
    createPairError.value = '目标价必须落在目标下限与上限之间'
    return
  }
  try {
    const created = await fxAdminApi.createPair({
      currency_code: f.currency_code,
      currency_name: f.currency_name,
      status: f.status,
      gold_reserve: f.gold_reserve,
      foreign_reserve: f.foreign_reserve,
      target_price: f.target_price,
      initial_price: f.initial_price,
      target_min: f.target_min,
      target_max: f.target_max,
      buy_fee_rate: f.buy_fee_rate,
      sell_fee_rate: f.sell_fee_rate,
    })
    await loadPairs()
    await loadPublicSnapshots()
    msg.success(`已创建货币对 ${created.currency_code}（草稿，可注资后开市）`)
  } catch (e) {
    createPairError.value = mapFxError(e, '创建货币对失败')
  }
}

async function changePairStatus(pair: FxPairPublic, status: FxPairStatus) {
  try {
    await fxAdminApi.updatePair(pair.id, { status })
    await loadPairs()
    msg.success(`${pair.currency_code} 状态已改为 ${combinePairStatus(status)}`)
  } catch (e) {
    msg.error(mapFxError(e, '状态更新失败'))
  }
}

async function fundPair(withdraw: boolean) {
  const id = fundForm.value.pair_id
  if (!id) {
    msg.error('请先选择货币对')
    return
  }
  const gold = toNumber(fundForm.value.gold_amount)
  const foreign = toNumber(fundForm.value.foreign_amount)
  if (!Number.isFinite(gold) || !Number.isFinite(foreign) || gold < 0 || foreign < 0 || (gold === 0 && foreign === 0)) {
    msg.error('注资/撤资金额必须为非负且至少一项为正')
    return
  }
  try {
    const body = { gold_amount: fundForm.value.gold_amount, foreign_amount: fundForm.value.foreign_amount }
    if (withdraw) await fxAdminApi.withdrawPair(id, body)
    else await fxAdminApi.fundPair(id, body)
    await loadPairs()
    await loadPublicSnapshots()
    msg.success(withdraw ? '撤资完成，已写入审计' : '注资完成，已写入审计')
  } catch (e) {
    msg.error(mapFxError(e, withdraw ? '撤资失败' : '注资失败'))
  }
}

// ── 事件 ──
async function createEvent() {
  eventError.value = null
  const f = eventForm.value
  if (!f.pair_id) {
    eventError.value = '请选择货币对'
    return
  }
  if (!f.title.trim()) {
    eventError.value = '请填写公开标题'
    return
  }
  const shock = toNumber(f.shock_ratio)
  const first = toNumber(f.first_reaction_ratio)
  const budget = toNumber(f.budget)
  const window = Math.trunc(toNumber(f.window_sec))
  const cap = f.kind === 'black_swan' ? 0.2 : 0.05
  if (!Number.isFinite(shock) || Math.abs(shock) > cap) {
    eventError.value = `冲击幅度必须在 ±${(cap * 100).toFixed(0)}% 以内`
    return
  }
  if (!Number.isFinite(first) || first < 0.1 || first > 0.9) {
    eventError.value = '首轮反应比例必须在 0.1–0.9 之间'
    return
  }
  if (!Number.isFinite(window) || window < 30 || window > 1800) {
    eventError.value = '事件窗口必须在 30–1800 秒之间'
    return
  }
  if (!Number.isFinite(budget) || budget <= 0) {
    eventError.value = '事件预算必须为正数'
    return
  }
  try {
    await fxAdminApi.createEvent({
      pair_id: f.pair_id,
      title: f.title,
      body: f.body,
      kind: f.kind,
      shock_ratio: f.shock_ratio,
      first_reaction_ratio: f.first_reaction_ratio,
      window_sec: window,
      budget: f.budget,
      scheduled_at: toIsoOrNull(f.scheduled_at),
    })
    f.title = ''
    f.body = ''
    await loadEvents()
    msg.success('事件草稿已创建（未发布）')
  } catch (e) {
    eventError.value = mapFxError(e, '创建事件失败')
  }
}

async function publishEvent(event: FxEventAdmin) {
  try {
    await fxAdminApi.publishEvent(event.id)
    await loadEvents()
    await loadPublicSnapshots()
    msg.success('事件已发布并完成首轮干预')
  } catch (e) {
    msg.error(mapFxError(e, '发布事件失败'))
  }
}

async function cancelEvent(event: FxEventAdmin) {
  try {
    await fxAdminApi.cancelEvent(event.id)
    await loadEvents()
    msg.success('事件已取消')
  } catch (e) {
    msg.error(mapFxError(e, '取消事件失败'))
  }
}

// ── 干预日志 ──
async function loadInterventions() {
  const id = interventionPairId.value
  if (!id) {
    interventions.value = []
    return
  }
  try {
    interventions.value = await fxAdminApi.listInterventions(id)
  } catch (e) {
    msg.error(mapFxError(e, '干预日志加载失败'))
  }
}

function toIsoOrNull(value: string): string | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  const time = new Date(trimmed).getTime()
  if (!Number.isFinite(time)) return null
  return new Date(time).toISOString()
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isFinite(d.getTime()) ? d.toLocaleString() : '—'
}

onMounted(async () => {
  await loadAll()
  await loadInterventions()
})
</script>

<template>
  <div class="fx-admin">
    <h1 class="page-title">FX 管理</h1>

    <div v-if="loading" class="fx-state">管理数据加载中…</div>
    <div v-else-if="error" class="fx-state fx-state-error">
      {{ error }}
      <button class="btn-secondary" @click="loadAll">重试</button>
    </div>

    <template v-else>
      <div class="fx-gate" :class="{ on: gateEnabled }">
        <div>
          <strong>FX 总闸：{{ gateEnabled ? '已开启' : '关闭（默认）' }}</strong>
          <p>
            总闸关闭时不出计划事件、不做噪声/常规干预、不允许买卖；只读行情与新闻仍可用。
            生产部署前必须保持关闭。
          </p>
        </div>
        <button class="btn-primary" @click="toggleGate">
          {{ gateEnabled ? '关闭总闸' : '开启总闸' }}
        </button>
      </div>

      <!-- 配置与预算 -->
      <section class="fx-panel">
        <h2>配置 / 预算</h2>
        <div class="fx-config-grid">
          <label v-for="key in CONFIG_ORDER" :key="key" class="fx-config-item">
            <span>{{ CONFIG_LABELS[key] }}<code>{{ key }}</code></span>
            <div class="fx-config-row">
              <input v-model="configDraft[key]" class="fx-input" :disabled="key === 'fx_enabled'" />
              <button class="btn-sm" @click="saveConfigValue(key, configDraft[key] ?? '')">保存</button>
            </div>
          </label>
        </div>
      </section>

      <!-- 货币对 -->
      <section class="fx-panel">
        <div class="fx-panel-head">
          <h2>货币对 / 池子 / 系统储备</h2>
          <button class="btn-secondary" @click="loadPairs().then(loadPublicSnapshots)">刷新</button>
        </div>
        <p class="fx-hint">
          数据来自 GET /api/v1/admin/fx/pairs（含草稿与系统 treasury），刷新后不再丢失运营状态。
          池子买卖价/价差来自公开 snapshot，单位统一为「金 / 1 外币」：买入价为 ask、卖出价为 bid，
          价差 = 买入价 − 卖出价 ≥ 0。
        </p>
        <div class="table-wrap">
          <table class="fx-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>币种</th>
                <th>状态</th>
                <th>池子（金 / 外币）</th>
                <th>系统 treasury（金 / 外币）</th>
                <th>今日支出</th>
                <th>公开买卖价 / 价差</th>
                <th>目标价 / 区间</th>
                <th>费率（买 / 卖）</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="p in visiblePairs" :key="p.id">
                <td>{{ p.id }}</td>
                <td>{{ p.currency_name }}（{{ p.currency_code }}）</td>
                <td>{{ combinePairStatus(p.status) }}</td>
                <td>
                  <template v-if="detailOf(p.id)">
                    {{ formatFxAmount(detailOf(p.id)!.gold_reserve) }} /
                    {{ formatFxAmount(detailOf(p.id)!.foreign_reserve) }}
                  </template>
                  <template v-else>—</template>
                </td>
                <td>
                  <template v-if="treasuryOf(p.id)">
                    {{ formatFxAmount(treasuryOf(p.id)!.gold_balance) }} /
                    {{ formatFxAmount(treasuryOf(p.id)!.foreign_balance) }}
                  </template>
                  <template v-else>—</template>
                </td>
                <td>
                  <template v-if="treasuryOf(p.id)">{{ formatFxAmount(treasuryOf(p.id)!.daily_spend) }}</template>
                  <template v-else>—</template>
                </td>
                <td>
                  <template v-if="snapshotOf(p.id)">
                    {{ formatFxAmount(snapshotOf(p.id)!.buy_price) }} /
                    {{ formatFxAmount(snapshotOf(p.id)!.sell_price) }}
                    （差 {{ formatFxAmount(snapshotOf(p.id)!.spread) }}）
                  </template>
                  <template v-else>—</template>
                </td>
                <td>
                  <template v-if="detailOf(p.id)">
                    {{ formatFxAmount(detailOf(p.id)!.target_price) }}
                    （{{ formatFxAmount(detailOf(p.id)!.target_min) }}–{{ formatFxAmount(detailOf(p.id)!.target_max) }}）
                  </template>
                  <template v-else>—</template>
                </td>
                <td>
                  <template v-if="detailOf(p.id)">
                    {{ formatFxAmount(detailOf(p.id)!.buy_fee_rate, 4) }} /
                    {{ formatFxAmount(detailOf(p.id)!.sell_fee_rate, 4) }}
                  </template>
                  <template v-else>—</template>
                </td>
                <td class="fx-actions-cell">
                  <template v-if="p.status !== 'trading'">
                    <button class="btn-sm" @click="changePairStatus(p, 'trading')">开市</button>
                  </template>
                  <template v-else>
                    <button class="btn-sm" @click="changePairStatus(p, 'paused')">暂停</button>
                  </template>
                  <button v-if="p.status !== 'closed'" class="btn-sm" @click="changePairStatus(p, 'closed')">关闭</button>
                </td>
              </tr>
              <tr v-if="visiblePairs.length === 0">
                <td colspan="10" class="fx-empty-cell">还没有货币对，请先创建草稿并注资</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="fx-form-grid">
          <div class="fx-form-card">
            <h3>创建货币对（草稿）</h3>
            <label>币种代码<input v-model="pairForm.currency_code" class="fx-input" /></label>
            <label>币种名称<input v-model="pairForm.currency_name" class="fx-input" /></label>
            <label>金圆券储备<input v-model="pairForm.gold_reserve" class="fx-input" /></label>
            <label>外币储备<input v-model="pairForm.foreign_reserve" class="fx-input" /></label>
            <label>目标价<input v-model="pairForm.target_price" class="fx-input" /></label>
            <label>初始价<input v-model="pairForm.initial_price" class="fx-input" /></label>
            <label>目标下限<input v-model="pairForm.target_min" class="fx-input" /></label>
            <label>目标上限<input v-model="pairForm.target_max" class="fx-input" /></label>
            <label>买入费率<input v-model="pairForm.buy_fee_rate" class="fx-input" /></label>
            <label>卖出费率<input v-model="pairForm.sell_fee_rate" class="fx-input" /></label>
            <p v-if="createPairError" class="fx-error">{{ createPairError }}</p>
            <button class="btn-primary" @click="createPair">创建草稿</button>
          </div>

          <div class="fx-form-card">
            <h3>注资 / 撤资</h3>
            <label>
              货币对
              <select v-model.number="fundForm.pair_id" class="fx-input">
                <option v-for="p in visiblePairs" :key="p.id" :value="p.id">
                  {{ p.currency_name }}（{{ p.currency_code }}）
                </option>
              </select>
            </label>
            <label>金圆券数量<input v-model="fundForm.gold_amount" class="fx-input" /></label>
            <label>外币数量<input v-model="fundForm.foreign_amount" class="fx-input" /></label>
            <div class="fx-form-actions">
              <button class="btn-primary" @click="fundPair(false)">注资</button>
              <button class="btn-secondary" @click="fundPair(true)">撤资</button>
            </div>
            <p class="fx-hint">注资/撤资会写站内发行或回收审计，不能直接改池子储备字段。</p>
          </div>
        </div>
      </section>

      <!-- 事件 -->
      <section class="fx-panel">
        <div class="fx-panel-head">
          <h2>宏观事件</h2>
          <button class="btn-secondary" @click="loadEvents">刷新</button>
        </div>

        <div class="fx-form-card fx-event-form">
          <h3>新建事件草稿</h3>
          <div class="fx-event-grid">
            <label>
              货币对
              <select v-model.number="eventForm.pair_id" class="fx-input">
                <option v-for="p in visiblePairs" :key="p.id" :value="p.id">
                  {{ p.currency_name }}（{{ p.currency_code }}）
                </option>
              </select>
            </label>
            <label>
              类型
              <select v-model="eventForm.kind" class="fx-input">
                <option v-for="k in eventKindOptions" :key="k.value" :value="k.value">{{ k.label }}</option>
              </select>
            </label>
            <label>公开标题<input v-model="eventForm.title" class="fx-input" /></label>
            <label>计划发布（UTC，可空）<input v-model="eventForm.scheduled_at" class="fx-input" placeholder="2026-01-01T00:00" /></label>
            <label>冲击幅度（如 0.01 = 1%）<input v-model="eventForm.shock_ratio" class="fx-input" /></label>
            <label>首轮反应比例<input v-model="eventForm.first_reaction_ratio" class="fx-input" /></label>
            <label>窗口（30–1800 秒）<input v-model.number="eventForm.window_sec" type="number" class="fx-input" /></label>
            <label>事件预算<input v-model="eventForm.budget" class="fx-input" /></label>
          </div>
          <label class="fx-body-field">公开正文<textarea v-model="eventForm.body" class="fx-input" rows="3"></textarea></label>
          <p v-if="eventError" class="fx-error">{{ eventError }}</p>
          <button class="btn-primary" @click="createEvent">保存草稿</button>
        </div>

        <div v-for="ev in events" :key="ev.id" class="fx-event-card">
          <div class="fx-event-col">
            <div class="fx-event-col-head">公开新闻（玩家可见）</div>
            <div class="fx-event-title">#{{ ev.id }} {{ ev.title || '未命名' }}</div>
            <div class="fx-event-body">{{ ev.body }}</div>
            <div class="fx-event-meta">
              {{ ev.kind }} · {{ EVENT_STATUS_LABELS[ev.status] ?? ev.status }} ·
              发布 {{ formatTime(ev.published_at) }}
            </div>
          </div>
          <div class="fx-event-col fx-event-internal">
            <div class="fx-event-col-head">内部数值参数（仅管理员）</div>
            <div class="fx-event-meta">
              冲击 {{ formatFxAmount(ev.shock_ratio, 6) }} · 首轮 {{ formatFxAmount(ev.first_reaction_ratio, 4) }} ·
              窗口 {{ ev.window_sec ?? '—' }}s · 预算 {{ formatFxAmount(ev.budget) }}
            </div>
            <div class="fx-event-meta">
              目标 before/after：
              {{ snapString(ev.parameter_snapshot, 'target_before') }} →
              {{ snapString(ev.parameter_snapshot, 'target_after') }} ·
              spent {{ snapString(ev.parameter_snapshot, 'spent') }}
            </div>
            <div v-if="ev.scheduled_at" class="fx-event-meta">计划 {{ formatTime(ev.scheduled_at) }}</div>
            <div v-if="ev.error_message" class="fx-error">{{ ev.error_message }}</div>
          </div>
          <div class="fx-event-actions">
            <button
              v-if="ev.status === 'draft' || ev.status === 'scheduled'"
              class="btn-sm"
              @click="publishEvent(ev)"
            >
              立即发布
            </button>
            <button
              v-if="ev.status === 'draft' || ev.status === 'scheduled'"
              class="btn-sm"
              @click="cancelEvent(ev)"
            >
              取消
            </button>
          </div>
        </div>
        <p v-if="events.length === 0" class="fx-hint">暂无事件。草稿只有在总闸开启后才能发布。</p>
      </section>

      <!-- 干预日志 -->
      <section class="fx-panel">
        <div class="fx-panel-head">
          <h2>系统干预日志</h2>
          <div class="fx-config-row">
            <select v-model.number="interventionPairId" class="fx-input">
              <option v-for="p in visiblePairs" :key="p.id" :value="p.id">
                {{ p.currency_name }}（{{ p.currency_code }}）
              </option>
            </select>
            <button class="btn-secondary" @click="loadInterventions">加载</button>
          </div>
        </div>
        <div class="table-wrap">
          <table class="fx-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>方向</th>
                <th>投入</th>
                <th>产出</th>
                <th>成交后价格</th>
                <th>来源</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in interventions" :key="row.id">
                <td>{{ formatTime(row.created_at) }}</td>
                <td>{{ row.side }}</td>
                <td>{{ formatFxAmount(row.input_amount) }}</td>
                <td>{{ formatFxAmount(row.output_amount) }}</td>
                <td>{{ formatFxAmount(row.post_price) }}</td>
                <td>{{ row.source }}</td>
              </tr>
              <tr v-if="interventions.length === 0">
                <td colspan="6" class="fx-empty-cell">暂无系统干预记录</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.fx-admin {
  padding: 16px;
  max-width: 1200px;
  margin: 0 auto;
}
.page-title {
  font-size: 22px;
  font-weight: 700;
  margin: 0 0 16px;
}
.fx-state {
  border: 2px solid #000;
  padding: 24px;
  background: #fff;
  font-weight: 600;
  display: flex;
  gap: 12px;
  align-items: center;
}
.fx-state-error {
  color: var(--color-down, #dc2626);
}
.fx-gate {
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
.fx-gate.on {
  background: #fffbeb;
  border-color: #b45309;
}
.fx-gate p {
  margin: 4px 0 0;
  font-size: 12px;
  color: #555;
}
.fx-panel {
  border: 2px solid #000;
  background: #fff;
  padding: 16px;
  margin-bottom: 16px;
}
.fx-panel h2 {
  margin: 0 0 10px;
  font-size: 15px;
  font-weight: 800;
}
.fx-panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.fx-gap-note {
  border-left: 3px solid #b45309;
  background: #fffbeb;
  padding: 8px 10px;
  font-size: 12px;
  line-height: 1.6;
  color: #92400e;
  margin: 8px 0;
}
.fx-config-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 18px;
}
.fx-config-item > span {
  display: block;
  font-size: 12px;
  color: #444;
  margin-bottom: 3px;
}
.fx-config-item code {
  color: #999;
  margin-left: 6px;
  font-size: 11px;
}
.fx-config-row {
  display: flex;
  gap: 6px;
  align-items: center;
}
.fx-input {
  border: 2px solid #000;
  padding: 6px 8px;
  font-family: ui-monospace, monospace;
  font-size: 13px;
  background: #fff;
  width: 100%;
}
.fx-input:disabled {
  background: #f0f0f0;
  color: #777;
}
.fx-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin-top: 16px;
}
.fx-form-card {
  border: 1.5px solid #000;
  padding: 12px;
  background: #fafafa;
}
.fx-form-card h3 {
  margin: 0 0 10px;
  font-size: 14px;
}
.fx-form-card label,
.fx-event-grid label,
.fx-body-field {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 12px;
  color: #444;
  margin-bottom: 8px;
}
.fx-form-actions {
  display: flex;
  gap: 8px;
}
.fx-event-form {
  margin-bottom: 14px;
}
.fx-event-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 14px;
}
.fx-event-card {
  border: 1.5px solid #000;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
  gap: 12px;
  padding: 12px;
  margin-bottom: 10px;
}
.fx-event-col-head {
  font-size: 10px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #888;
  margin-bottom: 4px;
}
.fx-event-title {
  font-weight: 700;
  font-size: 13px;
}
.fx-event-body {
  font-size: 12px;
  color: #444;
  line-height: 1.5;
  margin-top: 2px;
}
.fx-event-meta {
  font-size: 11px;
  color: #777;
  margin-top: 4px;
  line-height: 1.5;
  word-break: break-word;
}
.fx-event-internal {
  border-left: 1px dashed #999;
  padding-left: 12px;
}
.fx-event-actions {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.fx-actions-cell {
  min-width: 160px;
}
.fx-error {
  color: var(--color-down, #dc2626);
  font-size: 12px;
  font-weight: 600;
  margin: 4px 0;
}
.fx-hint {
  font-size: 12px;
  color: #777;
  line-height: 1.5;
  margin: 8px 0 0;
}
.table-wrap {
  margin-top: 8px;
  overflow-x: auto;
  border: 2px solid #000;
}
.fx-table {
  width: 100%;
  border-collapse: collapse;
  background: #fff;
}
.fx-table th,
.fx-table td {
  border: 1px solid #ddd;
  padding: 6px 10px;
  text-align: left;
  white-space: nowrap;
  font-size: 12px;
}
.fx-table th {
  background: #000;
  color: #fff;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-size: 11px;
}
.fx-empty-cell {
  text-align: center;
  color: #888;
}
@media (max-width: 900px) {
  .fx-config-grid,
  .fx-form-grid,
  .fx-event-grid {
    grid-template-columns: 1fr;
  }
  .fx-event-card {
    grid-template-columns: 1fr;
  }
  .fx-event-internal {
    border-left: none;
    padding-left: 0;
    border-top: 1px dashed #999;
    padding-top: 8px;
  }
}
</style>
